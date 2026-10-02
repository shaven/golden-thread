"""gt_bench.py recall -- does vault lookup find the right page? (0.19.0, request recall-benchmark)

A fixed question set against a synthetic fixture vault (tests/fixtures/recall-bench, generated
by its make_fixture.py; no private content). Per retriever: recall@1/3/10, files read and an
approximate token count per question, wall time. A retriever that cannot load or raises is
could-not-run, never zero recall.
"""
import json
import unittest
from pathlib import Path

from _harness import Sandbox, SCRIPTS, REPO

BENCH = SCRIPTS / "gt_bench.py"
FIXTURE = REPO / "tests" / "fixtures" / "recall-bench"


class RecallBench(Sandbox):

    def bench(self, *args):
        p = self.py(BENCH, "recall", "--fixture", FIXTURE, "--json", *args)
        self.assertNotIn("Traceback", p.stderr)
        return p, json.loads(p.stdout)

    def stub(self, body):
        f = self.tmp / ("r%d.py" % len(list(self.tmp.glob("r*.py"))))
        f.write_text(body)
        return f

    def test_the_fixture_has_30_questions_across_every_level(self):
        qs = json.loads((FIXTURE / "questions.json").read_text())["questions"]
        self.assertGreaterEqual(len(qs), 30)
        self.assertEqual({q["level"] for q in qs}, {"project", "knowledge", "global-memory"})
        for q in qs:
            self.assertTrue((FIXTURE / "vault" / q["expected"]).is_file(), q)

    def test_the_default_retriever_reports_recall_and_per_question_cost(self):
        p, d = self.bench()
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        (r,) = d["retrievers"]
        self.assertEqual((r["name"], r["status"]), ("keyword", "ran"))
        for k in ("recall@1", "recall@3", "recall@10"):
            self.assertTrue(0.0 <= r[k] <= 1.0, r)
        self.assertLessEqual(r["recall@1"], r["recall@3"])
        self.assertLessEqual(r["recall@3"], r["recall@10"])
        self.assertEqual(len(r["questions"]), 36)
        q = r["questions"][0]
        self.assertGreater(q["files_read"], 0)
        self.assertGreater(q["approx_tokens"], 0)

    def test_a_perfect_retriever_scores_one(self):
        perfect = self.stub(
            "import json, pathlib\n"
            "Q = json.loads((pathlib.Path(%r) / 'questions.json').read_text())['questions']\n"
            "def retrieve(question, vault, k=10):\n"
            "    return [q['expected'] for q in Q if q['question'] == question]\n"
            % str(FIXTURE))
        p, d = self.bench("--retriever", str(perfect))
        self.assertEqual(p.returncode, 0)
        self.assertEqual(d["retrievers"][0]["recall@1"], 1.0)

    def test_a_retriever_that_raises_is_could_not_run_never_zero(self):
        broken = self.stub("def retrieve(question, vault, k=10):\n    raise RuntimeError('x')\n")
        p, d = self.bench("--retriever", str(broken))
        self.assertNotEqual(p.returncode, 0)
        r = d["retrievers"][0]
        self.assertEqual(r["status"], "could-not-run")
        self.assertIsNone(r.get("recall@1"))

    def test_a_retriever_that_does_not_load_is_could_not_run(self):
        p, d = self.bench("--retriever", str(self.tmp / "missing.py"))
        self.assertNotEqual(p.returncode, 0)
        self.assertEqual(d["retrievers"][0]["status"], "could-not-run")

    def test_two_retrievers_compare_in_one_table(self):
        perfect = self.stub(
            "import json, pathlib\n"
            "Q = json.loads((pathlib.Path(%r) / 'questions.json').read_text())['questions']\n"
            "def retrieve(question, vault, k=10):\n"
            "    return [q['expected'] for q in Q if q['question'] == question]\n"
            % str(FIXTURE))
        p, d = self.bench("--retriever", "keyword", "--retriever", str(perfect))
        self.assertEqual([r["name"] for r in d["retrievers"]], ["keyword", perfect.stem])
        p = self.py(BENCH, "recall", "--fixture", FIXTURE, "--retriever", "keyword",
                    "--retriever", str(perfect))
        self.assertIn("recall@3", p.stdout)
        self.assertIn("keyword", p.stdout)
        self.assertIn(perfect.stem, p.stdout)

    def test_the_result_row_is_written_beside_the_bench_output(self):
        self.bench()
        rows = (self.home / ".claude" / "golden-thread" / "bench-recall.jsonl").read_text()
        self.assertIn('"recall@3"', rows)


if __name__ == "__main__":
    unittest.main()
