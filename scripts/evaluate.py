"""Evaluate deterministic matcher statuses against generated ground truth."""
import csv
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))

from app.matcher import reconcile


def read_csv(path):
    with open(path, newline="", encoding="utf-8-sig") as handle:
        return list(csv.DictReader(handle))


def main():
    data_dir = os.path.join(os.path.dirname(__file__), "..", "backend", "data")
    truth = read_csv(os.path.join(data_dir, "ground_truth.csv"))
    report = reconcile(read_csv(os.path.join(data_dir, "gstr2b.csv")), read_csv(os.path.join(data_dir, "purchase_ledger.csv")))
    predicted = {}
    priority = {"DUPLICATE_IN_LEDGER": 3, "FUZZY_MATCH": 2, "MATCHED": 1}
    for row in report["results"]:
        key = (row["gstin"], row["gstr2b_invoice"] or row["ledger_invoice"])
        status = row["status"]
        if priority.get(status, 0) >= priority.get(predicted.get(key), 0):
            predicted[key] = status
    mapping = {
        "clean_match": "MATCHED", "amount_mismatch": "AMOUNT_MISMATCH",
        "missing_in_ledger": "MISSING_IN_LEDGER", "missing_in_2b": "MISSING_IN_2B",
        "duplicate_in_ledger": "DUPLICATE_IN_LEDGER", "invoice_number_typo": "FUZZY_MATCH",
        "date_mismatch": "DATE_MISMATCH",
    }
    accepted_statuses = {
        "date_mismatch": {"DATE_MISMATCH", "CROSS_PERIOD_MATCH"},
    }
    labels = list(mapping)
    matrix = {actual: {expected: 0 for expected in labels} for actual in labels}
    for row in truth:
        actual = row["scenario"]
        status = predicted.get((row["gstin"], row["invoice_number"]), "UNDETECTED")
        expected = next(
            (
                scenario
                for scenario, mapped in mapping.items()
                if status in accepted_statuses.get(scenario, {mapped})
            ),
            "UNDETECTED",
        )
        matrix[actual][expected] = matrix[actual].get(expected, 0) + 1
    print("Scenario evaluation (rows=truth, columns=prediction)")
    print("actual\\predicted\t" + "\t".join(labels + ["UNDETECTED"]))
    for actual in labels:
        print(actual + "\t" + "\t".join(str(matrix[actual].get(expected, 0)) for expected in labels + ["UNDETECTED"]))
    metrics = {}
    for scenario, status in mapping.items():
        tp = matrix[scenario].get(scenario, 0)
        fp = sum(matrix[other].get(scenario, 0) for other in labels if other != scenario)
        fn = sum(matrix[scenario].get(other, 0) for other in labels + ["UNDETECTED"] if other != scenario)
        precision = tp / (tp + fp) if tp + fp else 0
        recall = tp / (tp + fn) if tp + fn else 0
        f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0
        metrics[scenario] = {"precision": precision, "recall": recall, "f1": f1,
                             "support": sum(matrix[scenario].values())}
        print(f"{scenario}: precision={precision:.3f} recall={recall:.3f} F1={f1:.3f}")

    macro = {name: sum(item[name] for item in metrics.values()) / len(metrics)
             for name in ("precision", "recall", "f1")}
    total_support = sum(item["support"] for item in metrics.values())
    weighted = {name: sum(item[name] * item["support"] for item in metrics.values()) / total_support
                for name in ("precision", "recall", "f1")}
    print("\nAggregate metrics")
    print("average\tprecision\trecall\tF1")
    print(f"macro\t{macro['precision']:.3f}\t{macro['recall']:.3f}\t{macro['f1']:.3f}")
    print(f"weighted\t{weighted['precision']:.3f}\t{weighted['recall']:.3f}\t{weighted['f1']:.3f}")


if __name__ == "__main__":
    main()
