"""Tests for hybrid rule + semantic Jira ticket matching."""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from assign_ticket_links import (  # noqa: E402
    DEFAULT_MIN_SCORE,
    apply_semantic_matches,
    assign_links,
    pick_issue,
    score_match,
)
from semantic_match_tickets import (  # noqa: E402
    build_prompt,
    jobs_needing_semantic_match,
    validate_matches,
)


def _issue(key: str, summary: str = "", description: str = "") -> dict:
    return {
        "key": key,
        "summary": summary,
        "description": description,
        "status": "In Progress",
        "sprint_name": "",
        "ticket_url": f"https://example.atlassian.net/browse/{key}",
        "is_open": True,
    }


class TestScoreMatch(unittest.TestCase):
    def test_stopword_only_overlap_scores_zero(self) -> None:
        job = {"job_id": "2594913", "root_cause_summary": "Failed for the cluster"}
        issue = _issue("GPTEINFRA-1", summary="SSL/DNS update for the workshop")
        self.assertEqual(score_match(job, issue), 0)

    def test_role_match_meets_threshold(self) -> None:
        job = {"failing_role": "infra-openshift-cnv-resources"}
        issue = _issue("GPTEINFRA-2", summary="Fix infra-openshift-cnv-resources on ocpv01")
        self.assertGreaterEqual(score_match(job, issue), DEFAULT_MIN_SCORE)


class TestAssignLinks(unittest.TestCase):
    def test_rule_match_sets_method_rule(self) -> None:
        role = "infra-openshift-cnv-resources"
        report = {"job_summaries": [{"job_id": "1", "failing_role": role}]}
        jira_data = {"issues": [_issue("GPTEINFRA-1", summary=f"Fix {role}")]}
        result = assign_links(report, jira_data, min_score=DEFAULT_MIN_SCORE)
        job = result["job_summaries"][0]
        self.assertEqual(job["ticket_match_method"], "rule")
        self.assertIsNone(job["ticket_match_confidence"])
        self.assertIsNotNone(job["ticket_link"])

    def test_below_threshold_without_semantic_stays_null(self) -> None:
        report = {
            "job_summaries": [
                {"job_id": "2594913", "root_cause_summary": "Missing PVC"},
            ]
        }
        jira_data = {"issues": [_issue("GPTEINFRA-1", summary="SSL/DNS update for workshop")]}
        result = assign_links(report, jira_data, min_score=DEFAULT_MIN_SCORE)
        job = result["job_summaries"][0]
        self.assertIsNone(job["ticket_link"])
        self.assertIsNone(job["ticket_match_method"])

    def test_semantic_match_applied_when_rule_fails(self) -> None:
        report = {
            "job_summaries": [
                {
                    "job_id": "2594913",
                    "catalog_item": "zt-rhelbu.zt-rhel-bu-lab-developer-cnv.prod",
                    "platform": "openshift_cnv",
                    "root_cause_summary": "Missing PVC in cnv-images",
                }
            ]
        }
        issues = [
            _issue(
                "GPTEINFRA-16879",
                summary="SSL/DNS update: OpenShift Virtualization (CNV)",
            ),
            _issue("GPTEINFRA-16990", summary="SSL/DNS update: Migration Toolkit"),
        ]
        semantic = {
            "matches": [
                {
                    "job_id": "2594913",
                    "ticket_key": "GPTEINFRA-16879",
                    "confidence": "high",
                    "rationale": "CNV developer lab maps to OpenShift Virtualization workshop",
                }
            ]
        }
        result = assign_links(
            report,
            {"issues": issues},
            min_score=DEFAULT_MIN_SCORE,
            semantic_matches=semantic,
        )
        job = result["job_summaries"][0]
        self.assertEqual(job["ticket_match_method"], "semantic")
        self.assertEqual(job["ticket_match_confidence"], "high")
        self.assertIn("GPTEINFRA-16879", job["ticket_link"])
        self.assertEqual(
            job["ticket_match_rationale"],
            "CNV developer lab maps to OpenShift Virtualization workshop",
        )

    def test_rule_match_not_overridden_by_semantic(self) -> None:
        role = "infra-openshift-cnv-resources"
        report = {"job_summaries": [{"job_id": "1", "failing_role": role}]}
        issues = [_issue("GPTEINFRA-RULE", summary=f"Fix {role}")]
        semantic = {
            "matches": [
                {
                    "job_id": "1",
                    "ticket_key": "GPTEINFRA-OTHER",
                    "confidence": "high",
                    "rationale": "Should not apply",
                }
            ]
        }
        issues.append(_issue("GPTEINFRA-OTHER", summary="Other ticket"))
        result = assign_links(
            report,
            {"issues": issues},
            min_score=DEFAULT_MIN_SCORE,
            semantic_matches=semantic,
        )
        job = result["job_summaries"][0]
        self.assertEqual(job["ticket_match_method"], "rule")
        self.assertIn("GPTEINFRA-RULE", job["ticket_link"])

    def test_low_semantic_confidence_rejected(self) -> None:
        report = {"job_summaries": [{"job_id": "2594913", "root_cause_summary": "Missing PVC"}]}
        issues = [_issue("GPTEINFRA-16879", summary="OpenShift Virtualization (CNV)")]
        semantic = {
            "matches": [
                {
                    "job_id": "2594913",
                    "ticket_key": "GPTEINFRA-16879",
                    "confidence": "low",
                    "rationale": "Weak match",
                }
            ]
        }
        result = assign_links(
            report,
            {"issues": issues},
            semantic_matches=semantic,
            semantic_min_confidence="medium",
        )
        job = result["job_summaries"][0]
        self.assertIsNone(job["ticket_link"])


class TestSemanticMatchTickets(unittest.TestCase):
    def test_jobs_needing_semantic_match_filters_by_score(self) -> None:
        jobs = [
            {"job_id": "2594913", "failing_role": "infra-openshift-cnv-resources"},
            {"job_id": "2594914", "root_cause_summary": "unrelated failure"},
        ]
        issues = [
            _issue("GPTEINFRA-1", summary="Fix infra-openshift-cnv-resources"),
            _issue("GPTEINFRA-2", summary="Unrelated ticket"),
        ]
        unmatched = jobs_needing_semantic_match(jobs, issues, min_score=DEFAULT_MIN_SCORE)
        self.assertEqual([job["job_id"] for job in unmatched], ["2594914"])

    def test_validate_matches_rejects_unknown_ticket(self) -> None:
        data = {
            "matches": [
                {
                    "job_id": "2594913",
                    "ticket_key": "GPTEINFRA-99999",
                    "confidence": "high",
                    "rationale": "bad key",
                }
            ]
        }
        result = validate_matches(data, {"2594913"}, {"GPTEINFRA-16879"})
        self.assertEqual(result["matches"], [])

    def test_build_prompt_includes_job_and_issue_data(self) -> None:
        jobs = [{"job_id": "2594913", "catalog_item": "zt-rhel-bu-lab-developer-cnv.prod"}]
        issues = [_issue("GPTEINFRA-16879", summary="OpenShift Virtualization (CNV)")]
        prompt = build_prompt(jobs, issues)
        self.assertIn("2594913", prompt)
        self.assertIn("GPTEINFRA-16879", prompt)
        self.assertIn("OpenShift Virtualization", prompt)


class TestApplySemanticMatches(unittest.TestCase):
    def test_apply_semantic_matches_returns_count(self) -> None:
        jobs = [{"job_id": "2594913", "ticket_link": None}]
        issues = [_issue("GPTEINFRA-16879", summary="CNV workshop")]
        semantic = {
            "matches": [
                {
                    "job_id": "2594913",
                    "ticket_key": "GPTEINFRA-16879",
                    "confidence": "medium",
                    "rationale": "CNV lab",
                }
            ]
        }
        applied = apply_semantic_matches(jobs, semantic, issues, min_confidence="medium")
        self.assertEqual(applied, 1)
        self.assertEqual(jobs[0]["ticket_match_method"], "semantic")


class TestPickIssue(unittest.TestCase):
    def test_tie_breaking_preserves_issue_order(self) -> None:
        job = {"job_id": "2594913", "root_cause_summary": "unrelated failure"}
        issues = [_issue("GPTEINFRA-A"), _issue("GPTEINFRA-B")]
        best, score = pick_issue(job, issues)
        self.assertEqual(best["key"], "GPTEINFRA-A")
        self.assertEqual(score, 0)


if __name__ == "__main__":
    unittest.main()
