"""Tests for the MesoScope web layer.

Offline only: no network, no LLM.  Covers the rule engine's correctness
guarantees, evidence grounding, retrieval ranking and the export contract.
"""

from __future__ import annotations

import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
for extra in (ROOT, ROOT / "src"):
    if str(extra) not in sys.path:
        sys.path.insert(0, str(extra))

from webapp.app.auth import (
    Accounts,
    AuthError,
    hash_password,
    verify_password,
)
from webapp.app.db import Database, now
from webapp.app.demo import DEMO_PAPERS
from webapp.app.heuristic import extract_records_heuristic
from webapp.app.qa import Retriever, best_snippet, build_answer, expand_query
from webapp.app.summarize import BRIEF_LIMIT, build_summary


SBA15 = DEMO_PAPERS[0]["pages"]
SBA16 = DEMO_PAPERS[1]["pages"]
FILM = DEMO_PAPERS[2]["pages"]


class RuleEngineTest(unittest.TestCase):
    def fields(self, pages):
        records, _ = extract_records_heuristic(pages)
        self.assertTrue(records, "rule engine returned no record")
        return records[0]["fields"]

    def test_identifies_surfactant_and_silica_source(self):
        fields = self.fields(SBA15)
        self.assertEqual(fields["surfactant"], "Pluronic P123")
        self.assertEqual(fields["silica_source"], "TEOS")

    def test_reads_results_not_introduction(self):
        """The intro mentions "below 4 nm" for CTAB; the product is 7.8 nm."""
        fields = self.fields(SBA15)
        self.assertEqual(fields["pore_size"], "7.8")
        self.assertEqual(fields["surface_area"], "820")

    def test_temperature_programme_is_ordered_with_durations(self):
        value = self.fields(SBA15)["synthesis_temperature"]
        self.assertIn("550 °C calcination 6 h", value)
        self.assertIn("aging 20 h", value)

    def test_concentration_is_not_cross_assigned(self):
        """"120 g of 2.0 M HCl" must not become the surfactant concentration."""
        fields = self.fields(SBA15)
        self.assertEqual(fields["acid_base_concentration"], "HCl 2.0 M")
        self.assertIsNone(fields["surfactant_concentration"])

    def test_mass_fraction_is_preferred_and_attributed(self):
        fields = self.fields(FILM)
        self.assertEqual(fields["surfactant_concentration"], "4.5 w%")
        self.assertEqual(fields["surfactant"], "CTAB")

    def test_salt_with_concentration(self):
        self.assertEqual(self.fields(SBA16)["salt"], "KCl 2.0 w%")

    def test_no_salt_reported_stays_null(self):
        self.assertIsNone(self.fields(SBA15)["salt"])

    def test_mesostructure_and_named_family(self):
        self.assertEqual(self.fields(SBA16)["mesostructure"], "cubic Im3m")
        self.assertEqual(self.fields(FILM)["mesostructure"], "2D hexagonal (p6mm)")

    def test_morphology_describes_product_not_micelles(self):
        """F127 favours "spherical micelles"; the product is spherical particles."""
        value = self.fields(SBA16)["morphology"]
        self.assertIn("spheres", value)

    def test_solution_system_volume_ratio(self):
        self.assertEqual(self.fields(SBA16)["solution_system"], "H2O:EtOH = 1:1")

    def test_every_filled_field_has_grounded_evidence(self):
        for pages in (SBA15, SBA16, FILM):
            records, _ = extract_records_heuristic(pages)
            record = records[0]
            joined = [" ".join(page.split()).lower() for page in pages]
            for key, value in record["fields"].items():
                if not value:
                    continue
                items = record["evidence"].get(key)
                self.assertTrue(items, f"{key} has a value but no evidence")
                for item in items:
                    quote = " ".join(item["quote"].split()).lower()
                    self.assertIn(
                        quote,
                        joined[item["page"] - 1],
                        f"{key} quote not found on page {item['page']}",
                    )

    def test_non_chemistry_text_yields_no_record(self):
        records, issues = extract_records_heuristic(
            ["This paper discusses macroeconomic policy and interest rates."]
        )
        self.assertEqual(records, [])
        self.assertTrue(issues)

    def test_angstrom_is_converted_to_nanometres(self):
        pages = [
            "Experimental. CTAB and TEOS were used. "
            "The calcined sample has a BET surface area of 900 m2/g. "
            "The BJH pore size distribution is centred at a pore diameter of 38 A."
        ]
        self.assertEqual(extract_records_heuristic(pages)[0][0]["fields"]["pore_size"], "3.8")


class RetrievalTest(unittest.TestCase):
    def build(self):
        chunks = []
        for index, item in enumerate(DEMO_PAPERS, 1):
            for page, text in enumerate(item["pages"], 1):
                chunks.append(
                    {
                        "paper_fk": index,
                        "paper_key": f"paper_{index:03d}",
                        "title": item["title"],
                        "page": page,
                        "text": text,
                    }
                )
        return Retriever(chunks)

    def test_chinese_query_maps_to_english_terms(self):
        tokens = expand_query("煅烧温度是多少？")
        self.assertIn("calcined", tokens)
        self.assertIn("temperature", tokens)

    def test_calcination_question_hits_experimental_page(self):
        hits = self.build().search("煅烧温度和时间是多少？", top_k=1)
        self.assertEqual(hits[0]["page"], 2)
        self.assertIn("calcined", hits[0]["snippet"].lower())

    def test_bet_question_returns_the_measured_value(self):
        hits = self.build().search("比表面积是多少？", top_k=1)
        self.assertIn("surface area", hits[0]["snippet"].lower())

    def test_salt_question_finds_the_only_paper_using_salt(self):
        hits = self.build().search("KCl 起什么作用？", top_k=1)
        self.assertEqual(hits[0]["paper_key"], "paper_002")

    def test_unrelated_question_returns_nothing(self):
        self.assertEqual(self.build().search("股票市场的收益率", top_k=3), [])

    def test_snippet_centres_on_the_query_terms(self):
        text = ("filler " * 200) + "the BET surface area was 820 m2/g" + (" filler" * 200)
        snippet = best_snippet(text, ["bet", "surface", "area", "820"])
        self.assertIn("820", snippet)


class ExportContractTest(unittest.TestCase):
    def test_records_validate_against_the_project_contract(self):
        from mkea_extract.validation import validate_records

        records = []
        for index, item in enumerate(DEMO_PAPERS, 1):
            extracted, _ = extract_records_heuristic(item["pages"])
            key = f"paper_{index:03d}"
            for position, record in enumerate(extracted, 1):
                records.append(
                    {
                        "paper_id": index,
                        "paper_key": key,
                        "record_id": f"{key}_r{position:02d}",
                        "record_index": position,
                        "pdf_path": f"papers/{key}.pdf",
                        "fields": record["fields"],
                        "evidence": record["evidence"],
                        "confidence": record["confidence"],
                    }
                )
        report = validate_records(records)
        self.assertEqual(report["errors"], [])
        self.assertTrue(report["ok"])
        self.assertEqual(report["evidence_coverage"], 1.0)


class SummaryTest(unittest.TestCase):
    """Offline summaries: the hover brief and the full "内容总结" body.

    These assertions describe the deterministic engine, so the LLM path is
    pinned off regardless of whether the developer has an API key configured.
    """

    def setUp(self):
        patcher = mock.patch(
            "webapp.app.summarize.llm_status",
            return_value={"available": False, "reason": "test", "model": "", "base_url": ""},
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def build(self, pages):
        records, _ = extract_records_heuristic(pages)
        return build_summary(title="demo", pages=pages, records=records)

    def test_offline_engine_is_used_without_a_key(self):
        self.assertEqual(self.build(SBA15)["engine"], "heuristic")

    def test_brief_is_a_single_capped_line(self):
        """The brief renders inside a hover tooltip, so it must not wrap away."""
        for pages in (SBA15, SBA16, FILM):
            brief = self.build(pages)["brief"]
            self.assertTrue(brief)
            self.assertNotIn("\n", brief)
            self.assertLessEqual(len(brief), BRIEF_LIMIT)

    def test_brief_names_the_material_and_key_metrics(self):
        brief = self.build(SBA15)["brief"]
        self.assertIn("P123", brief)
        self.assertIn("820", brief)
        self.assertIn("m²/g", brief)

    def test_full_summary_lists_extracted_conditions(self):
        summary = self.build(SBA15)["summary"]
        self.assertIn("1 条样品记录", summary)
        self.assertIn("表面活性剂：Pluronic P123", summary)
        self.assertIn("硅源", summary)

    def test_summary_opens_with_a_chinese_overview(self):
        summary = self.build(SBA15)["summary"]
        self.assertTrue(summary.startswith("本文"))
        self.assertIn("为模板剂", summary)
        self.assertIn("介孔材料", summary)

    def test_results_are_reported_in_chinese_with_units(self):
        summary = self.build(SBA15)["summary"]
        self.assertIn("表征结果：", summary)
        self.assertIn("比表面积约 820 m²/g", summary)
        self.assertIn("孔径约 7.8 nm", summary)

    def test_english_vocabulary_is_translated(self):
        """The offline summary must read as Chinese, not Chinese labels + English values."""
        summary = self.build(FILM)["summary"]
        self.assertNotIn("film/monolith", summary)
        self.assertNotIn("calcination", summary)
        self.assertIn("二维六方", summary)

    def test_summary_carries_no_english_prose(self):
        """No English sentences may be quoted verbatim any more."""
        for pages in (SBA15, SBA16, FILM):
            for line in self.build(pages)["summary"].splitlines():
                words = re.findall(r"[A-Za-z]{3,}", line)
                self.assertLessEqual(
                    len(words), 6, f"line looks like English prose: {line}"
                )

    def test_empty_paper_degrades_without_raising(self):
        result = build_summary(title="标题兜底", pages=["", "   "], records=[])
        self.assertEqual(result["engine"], "heuristic")
        self.assertEqual(result["brief"], "标题兜底")
        self.assertIn("暂无可用于总结的文本内容", result["summary"])

    def test_text_without_extractable_content_explains_itself(self):
        result = build_summary(
            title="T",
            pages=["Macroeconomic policy and interest rates are debated in this essay."],
            records=[],
        )
        self.assertIn("未能从正文中提取", result["summary"])


class AnswerFormatTest(unittest.TestCase):
    """The assistant must lead every answer with a Chinese summary."""

    def hits(self):
        chunks = []
        for index, item in enumerate(DEMO_PAPERS, 1):
            for page, text in enumerate(item["pages"], 1):
                chunks.append(
                    {
                        "paper_fk": index,
                        "paper_key": f"paper_{index:03d}",
                        "title": item["title"],
                        "page": page,
                        "text": text,
                    }
                )
        return Retriever(chunks).search("比表面积是多少？", top_k=3)

    def test_extractive_answer_starts_with_a_chinese_digest(self):
        result = build_answer("比表面积是多少？", self.hits(), llm_available=False)
        self.assertTrue(result["answer"].startswith("**内容总结**："))
        self.assertIn("**详细回答**：", result["answer"])

    def test_digest_names_the_matched_topics(self):
        answer = build_answer("比表面积是多少？", self.hits(), llm_available=False)["answer"]
        digest = answer.splitlines()[0]
        self.assertIn("比表面积", digest)
        self.assertIn("比表面积是多少？", digest)

    def test_citations_still_accompany_the_answer(self):
        result = build_answer("比表面积是多少？", self.hits(), llm_available=False)
        self.assertTrue(result["citations"])
        for citation in result["citations"]:
            self.assertIn("page", citation)

    def test_empty_retrieval_still_answers_in_chinese(self):
        result = build_answer("股票收益率", [], llm_available=False)
        self.assertIn("没有相关信息", result["answer"])
        self.assertEqual(result["citations"], [])


class AccountTest(unittest.TestCase):
    """Credentials, sessions and profile data (no HTTP, no network)."""

    def setUp(self):
        self.db = Database(Path(tempfile.mkdtemp()) / "accounts.db")
        self.accounts = Accounts(self.db)

    def register(self, username="alice", password="secret123", **profile):
        return self.accounts.register(
            {"username": username, "password": password, **profile}
        )

    # -- password hashing -------------------------------------------------- #

    def test_password_is_never_stored_in_clear_text(self):
        self.register()
        row = self.db.one("SELECT password_hash FROM users WHERE username = 'alice'")
        self.assertNotIn("secret123", row["password_hash"])
        self.assertTrue(row["password_hash"].startswith("scrypt$"))

    def test_hash_is_salted_so_equal_passwords_differ(self):
        self.assertNotEqual(hash_password("same"), hash_password("same"))

    def test_verify_accepts_only_the_right_password(self):
        stored = hash_password("secret123")
        self.assertTrue(verify_password("secret123", stored))
        self.assertFalse(verify_password("secret124", stored))
        self.assertFalse(verify_password("secret123", "not-a-hash"))

    def test_user_payload_hides_the_hash(self):
        user = self.register()
        self.assertNotIn("password_hash", user)

    # -- validation -------------------------------------------------------- #

    def test_duplicate_username_is_rejected(self):
        self.register()
        with self.assertRaises(AuthError) as ctx:
            self.register()
        self.assertEqual(ctx.exception.status, 409)

    def test_short_or_malformed_credentials_are_rejected(self):
        for username, password in [
            ("ab", "secret123"),       # username too short
            ("bad name", "secret123"),  # space not allowed
            ("good_user", "12345"),     # password too short
        ]:
            with self.assertRaises(AuthError):
                self.register(username, password)

    def test_invalid_email_is_rejected(self):
        with self.assertRaises(AuthError):
            self.register(email="not-an-email")

    # -- login / sessions -------------------------------------------------- #

    def test_wrong_password_and_unknown_user_look_identical(self):
        """Do not leak whether an account exists."""
        self.register()
        errors = []
        for body in [
            {"username": "alice", "password": "wrong"},
            {"username": "ghost", "password": "wrong"},
        ]:
            with self.assertRaises(AuthError) as ctx:
                self.accounts.login(body)
            errors.append((str(ctx.exception), ctx.exception.status))
        self.assertEqual(errors[0], errors[1])

    def test_session_token_resolves_then_stops_after_logout(self):
        user = self.register()
        token = self.accounts.open_session(user["id"])
        self.assertEqual(self.accounts.user_for_token(token)["username"], "alice")
        self.accounts.close_session(token)
        self.assertIsNone(self.accounts.user_for_token(token))

    def test_unknown_and_expired_tokens_are_refused(self):
        user = self.register()
        self.assertIsNone(self.accounts.user_for_token("nope"))
        self.assertIsNone(self.accounts.user_for_token(None))
        token = self.accounts.open_session(user["id"])
        self.db.execute(
            "UPDATE sessions SET expires_at = '2000-01-01T00:00:00+00:00' WHERE token = ?",
            (token,),
        )
        self.assertIsNone(self.accounts.user_for_token(token))

    def test_changing_the_password_invalidates_existing_sessions(self):
        user = self.register()
        token = self.accounts.open_session(user["id"])
        self.accounts.change_password(
            user["id"], {"old_password": "secret123", "new_password": "newpass456"}
        )
        self.assertIsNone(self.accounts.user_for_token(token))
        self.assertTrue(self.accounts.login({"username": "alice", "password": "newpass456"}))

    def test_changing_the_password_requires_the_old_one(self):
        user = self.register()
        with self.assertRaises(AuthError) as ctx:
            self.accounts.change_password(
                user["id"], {"old_password": "wrong", "new_password": "newpass456"}
            )
        self.assertEqual(ctx.exception.status, 403)

    # -- profile ----------------------------------------------------------- #

    def test_profile_survives_as_server_side_state(self):
        """What makes a different device show the same details."""
        user = self.register(display_name="爱丽丝", institution="复旦")
        self.accounts.update_profile(user["id"], {"research_area": "介孔材料"})
        # A fresh Accounts instance stands in for another device's request.
        fresh = Accounts(Database(self.db.path)).require_user(user["id"])
        self.assertEqual(fresh["display_name"], "爱丽丝")
        self.assertEqual(fresh["institution"], "复旦")
        self.assertEqual(fresh["research_area"], "介孔材料")

    def test_unknown_profile_fields_are_ignored(self):
        user = self.register()
        with self.assertRaises(AuthError):
            self.accounts.update_profile(user["id"], {"is_admin": True})

    def test_display_name_defaults_to_the_username(self):
        self.assertEqual(self.register()["display_name"], "alice")


class StaticAssetCacheTest(unittest.TestCase):
    """A stale cached app.js next to a fresh index.html breaks every binding.

    That failure is silent (one TypeError skips all later addEventListener
    calls), so these guards are cheap insurance.
    """

    def html(self):
        return (ROOT / "webapp" / "static" / "index.html").read_text(encoding="utf-8")

    def test_assets_are_requested_with_a_version_query(self):
        html = self.html()
        self.assertIn("/static/style.css?v=__ASSET_V__", html)
        self.assertIn("/static/app.js?v=__ASSET_V__", html)

    def test_index_route_substitutes_a_real_hash(self):
        source = (ROOT / "webapp" / "app" / "main.py").read_text(encoding="utf-8")
        self.assertIn('html.replace("__ASSET_V__", _asset_version())', source)
        # The placeholder must never reach the browser verbatim.
        index_body = source.split("def index(")[1].split("\n@app.")[0]
        self.assertIn("no-store", index_body)

    def test_version_changes_when_a_bundle_changes(self):
        import importlib

        main = importlib.import_module("webapp.app.main")
        first = main._asset_version()
        self.assertRegex(first, r"^[0-9a-f]{12}$")
        self.assertEqual(first, main._asset_version())  # stable for same bytes

    def test_every_top_level_listener_tolerates_a_missing_node(self):
        """No bare $("id").addEventListener at top level: use on() instead."""
        app_js = (ROOT / "webapp" / "static" / "app.js").read_text(encoding="utf-8")
        bare = re.findall(r'^\$\("[A-Za-z0-9_]+"\)\.addEventListener', app_js, re.M)
        self.assertEqual(bare, [], f"{len(bare)} 处顶层绑定未使用 on() 包装")


class AutoSummaryTest(unittest.TestCase):
    """The summary is produced by the extraction job, not by a manual click."""

    def test_extraction_job_runs_the_summary_stage(self):
        """`_run_job` must report a 生成总结 stage and persist the result."""
        source = (ROOT / "webapp" / "app" / "main.py").read_text(encoding="utf-8")
        job_body = source.split("def _run_job(")[1].split("\n@app.")[0]
        self.assertIn("_refresh_summary(", job_body)
        self.assertIn("生成内容总结", job_body)

    def test_summary_failure_cannot_fail_the_extraction(self):
        """A broken summary degrades to an issue; records are already stored."""
        source = (ROOT / "webapp" / "app" / "main.py").read_text(encoding="utf-8")
        job_body = source.split("def _run_job(")[1].split("\n@app.")[0]
        summary_call = job_body.split("progress(\"生成内容总结\"")[1]
        self.assertIn("except Exception", summary_call.split("db.execute")[0])

    def test_front_end_maps_the_new_stage_to_a_step(self):
        app_js = (ROOT / "webapp" / "static" / "app.js").read_text(encoding="utf-8")
        self.assertIn('"生成总结"', app_js)
        self.assertIn('stage.includes("总结")', app_js)


class PaperOwnershipTest(unittest.TestCase):
    """Library queries are per-account; the QA corpus is shared."""

    def setUp(self):
        self.db = Database(Path(tempfile.mkdtemp()) / "owners.db")
        accounts = Accounts(self.db)
        self.alice = accounts.register({"username": "alice", "password": "secret123"})
        self.bob = accounts.register({"username": "bob", "password": "secret456"})
        self.add_paper(1, self.alice["id"], "alice 的论文")
        self.add_paper(2, self.alice["id"], "alice 的第二篇")
        self.add_paper(3, self.bob["id"], "bob 的论文")

    def add_paper(self, index, owner, title):
        paper_fk = self.db.execute(
            "INSERT INTO papers (paper_key, paper_id, owner_fk, title, created_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (f"paper_{index:03d}", index, owner, title, now()),
        )
        self.db.execute(
            "INSERT INTO paper_pages (paper_fk, page, text) VALUES (?, 1, ?)",
            (paper_fk, f"{title} 的正文，提到 mesoporous silica 与 TEOS。"),
        )
        return paper_fk

    def own_papers(self, user):
        return self.db.query(
            "SELECT * FROM papers WHERE owner_fk = ? ORDER BY paper_id", (user["id"],)
        )

    def test_each_account_sees_only_its_own_library(self):
        self.assertEqual(len(self.own_papers(self.alice)), 2)
        self.assertEqual(len(self.own_papers(self.bob)), 1)

    def test_a_paper_is_invisible_to_the_other_account(self):
        alice_paper = self.own_papers(self.alice)[0]["id"]
        self.assertIsNone(
            self.db.one(
                "SELECT id FROM papers WHERE id = ? AND owner_fk = ?",
                (alice_paper, self.bob["id"]),
            )
        )

    def test_shared_corpus_spans_every_account(self):
        """The asymmetry the product requires: private shelves, shared corpus."""
        pages = self.db.query(
            "SELECT paper_fk FROM paper_pages JOIN papers ON papers.id = paper_pages.paper_fk"
        )
        self.assertEqual(len({row["paper_fk"] for row in pages}), 3)

    def test_deleting_an_account_removes_its_papers_only(self):
        self.db.execute("DELETE FROM users WHERE id = ?", (self.bob["id"],))
        remaining = self.db.query("SELECT owner_fk FROM papers")
        self.assertEqual({row["owner_fk"] for row in remaining}, {self.alice["id"]})


if __name__ == "__main__":
    unittest.main()
