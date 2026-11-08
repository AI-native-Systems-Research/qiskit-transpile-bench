"""Freeze the crossed scaling panels and the explicitly separate corpus guards."""

from __future__ import annotations

import datetime as dt
import json
import os
import shutil
import sys
from collections import Counter
from pathlib import Path

from compare_distributions.collect import THREAD_ENV, digest, file_hash, seed_list, write_json
from compare_to_baseline.collect import (
    ARMS,
    EVOLVED_WAIT_TIME_WEIGHT,
    ROOT,
    host_identity,
    inspect_release_build,
    snapshot_source,
)

from qtb.benchpress.adapter import apply_adapter, write_configs
from qtb.benchpress.support import BACKEND_SEED, BENCHPRESS
from qtb.coordinator.benchpress import _source

FORMAT = "qtb-compare-to-baseline-benchpress/2"
LEVELS = (2, 3)
WIDTHS = (8, 16, 32, 64, 100)
REGRESSION_SEEDS = {
    "abstract-hamlib/OH:/ham_JW12,/linear/L2": 1275063328,
    "abstract-hamlib/OH:/ham_JW12,/linear/L3": 748532800,
    "abstract-qasmbench-large/cc_n32/linear/L2": 744744236,
    "abstract-qasmbench-large/cc_n32/linear/L3": 2775707439,
    "abstract-qasmbench-large/multiplier_n75/linear/L2": 1268144928,
    "abstract-qasmbench-large/multiplier_n75/linear/L3": 1534794620,
    "abstract-qasmbench-large/qugan_n71/linear/L2": 96041314,
    "abstract-qasmbench-large/qugan_n71/linear/L3": 2292208795,
    "abstract-qasmbench-large/swap_test_n83/linear/L2": 2835411017,
    "abstract-qasmbench-large/swap_test_n83/linear/L3": 679235954,
    "abstract-qasmbench-small/basis_trotter_n4/heavy-hex/L2": 4212569668,
    "abstract-qasmbench-small/basis_trotter_n4/heavy-hex/L3": 4039301381,
}


def corpus_selection(inventory):
    """Static source/target selection, never based on measured quality outcomes."""
    regression = {
        ("swap_test_n83", "linear"),
        ("swap_test_n83", "heavy-hex"),
        ("qugan_n71", "linear"),
        ("qugan_n71", "heavy-hex"),
        ("cc_n32", "linear"),
        ("cc_n32", "square"),
        ("multiplier_n75", "linear"),
        ("multiplier_n75", "square"),
        ("OH:/ham_JW12,", "linear"),
        ("basis_trotter_n4", "heavy-hex"),
    }
    applications = {
        "swap_test_n25",
        "swap_test_n41",
        "swap_test_n83",
        "qugan_n39",
        "qugan_n71",
        "qec_en_n5",
        "error_correctiond3_n5",
        "qec9xz_n17",
    }
    electronic = {
        "HF:/ham_parity10,",
        "OH:/ham_JW12,",
        "NH:/ham_JW-14,",
        "N2:/ham_JW-22,",
        "O3:/ham_BK22,",
        "Na2:/ham_JW24,",
    }
    selected = []
    for original in inventory:
        row = dict(original)
        name, topology = row["input_name"], row["topology"]
        panel = None
        if (name, topology) in regression:
            panel = "regressions"
        elif name in applications and topology == "heavy-hex":
            panel = "applications"
        elif name in ("test_circSU2_89_transpile", "test_circSU2_100_transpile"):
            panel = "applications"
        elif row["suite"] == "device-hamlib":
            if name in electronic:
                panel = "chemistry_electronic"
            elif (name.startswith("all-vib-h2co:") and row["input_qubits"] == 32) or (
                name.startswith("all-vib-cyclo_propene:") and row["input_qubits"] == 64
            ):
                panel = "chemistry_vibrational"
        if panel:
            row.update(
                panel=panel,
                scope="supplemental",
                execution_kind="upstream",
                family=row["suite"],
                family_id=row["suite"],
                selection_reason="Predeclared balanced-plan corpus coverage/guard",
            )
            selected.append(row)
    actual = {(r["input_name"], r["topology"]) for r in selected}
    if missing := regression - actual:
        raise ValueError(f"Required regression inputs missing: {sorted(missing)}")
    names = {r["input_name"] for r in selected}
    if missing := (applications | electronic) - names:
        raise ValueError(f"Required application/chemistry inputs missing: {sorted(missing)}")
    if sum(r["panel"] == "chemistry_vibrational" for r in selected) != 2:
        raise ValueError("Require both explicitly separate vibrational chemistry inputs")
    return sorted(selected, key=lambda r: (r["panel"], r["node_id"]))


def crossed_cases(inventory, *, include_supplemental=True):
    """Build all input/target/level cells; physical-width choices are in target specs."""
    cases = []
    for logical in inventory["inputs"]:
        width = logical["input_qubits"]
        name = logical["input_id"].rsplit("/", 1)[-1]
        for target in inventory["targets"]:
            panel = target["panel"]
            if panel not in ("general", "ibm") and not include_supplemental:
                continue
            if target.get("logical_qubits") not in (None, width):
                continue
            for level in LEVELS:
                row = dict(logical)
                row.update(target)
                row.update(
                    case_id=f"{panel}/{logical['family_id']}/{name}/{target['target_id']}/L{level}",
                    input_name=name,
                    suite=logical["family_id"],
                    input_qubits=width,
                    logical_qubits=width,
                    active_qubits=width,
                    family=logical["family"],
                    family_id=logical["family_id"],
                    panel=panel,
                    scope="core" if panel in ("general", "ibm") else "supplemental",
                    execution_kind="balanced",
                    optimization_level=level,
                    node_id=logical["input_id"],
                    constraint_form="target",
                    native_basis=target["native_2q_gate"],
                    options={
                        "approximation_degree": 1.0,
                        "initial_layout": None,
                        "qubits_initially_zero": True,
                    },
                )
                cases.append(row)
    validate_balance(cases)
    return cases


def validate_balance(cases):
    for panel in ("general", "ibm"):
        rows = [r for r in cases if r["panel"] == panel]
        if len(rows) != 720:
            raise ValueError(f"Balanced {panel} panel must have 720 cases, got {len(rows)}")
        cells = Counter((r["family_id"], r["input_qubits"], r["optimization_level"]) for r in rows)
        if len(cells) != 80 or set(cells.values()) != {9}:
            raise ValueError(f"Unbalanced family/width/level coverage in {panel}")
        expected_targets = (
            {"linear", "grid", "heavy_hex_patch"}
            if panel == "general"
            else {"FakeTorino", "FakeSherbrooke", "FakeWashingtonV2"}
        )
        if (
            {r["family_id"] for r in rows} != {f"F{i}" for i in range(1, 9)}
            or {r["input_qubits"] for r in rows} != set(WIDTHS)
            or {r["topology"] for r in rows} != expected_targets
        ):
            raise ValueError(f"Unexpected family/width/target identities in {panel}")
        by_input = {}
        for row in rows:
            by_input.setdefault(row["input_id"], []).append(row)
        if len(by_input) != 120 or any(
            len(members) != 6
            or {(r["topology"], r["optimization_level"]) for r in members}
            != {(target, level) for target in expected_targets for level in LEVELS}
            for members in by_input.values()
        ):
            raise ValueError(f"Missing input/target/level cross-product cells in {panel}")
        for row in rows:
            expected_width = 2 * row["input_qubits"] if panel == "general" else None
            if expected_width is not None and row["physical_qubits"] != expected_width:
                raise ValueError("Synthetic capacity does not match P=2n")


def _prepare_upstream(root, mirror, frozen_sources):
    from .collect import case_name, run_worker, worker_command

    # Reuse a verified pinned Git archive already present locally, avoiding a fetch.
    cached = ROOT / "output/compare_to_baseline_benchpress"
    if (
        mirror is None
        and (cached / "benchpress.tar").exists()
        and (cached / "manifest.json").exists()
    ):
        evidence = json.loads((cached / "manifest.json").read_text()).get("benchpress", {})
        if evidence.get("revision") == BENCHPRESS and file_hash(cached / "benchpress.tar") == (
            evidence.get("archive_sha256")
        ):
            shutil.copy2(cached / "benchpress.tar", root / "benchpress.tar")
    source = _source(
        root,
        "benchpress",
        "https://github.com/Qiskit/benchpress.git",
        BENCHPRESS,
        mirror or os.environ.get("QTB_BENCHPRESS_SOURCE"),
    )
    apply_adapter(source, root / "adapter.patch")
    write_configs(source, root / "configs", LEVELS)
    inventories = {}
    for arm in ARMS:
        destination = root / "preflight" / f"inventory-{arm}.json"
        run_worker(
            worker_command(root, arm, "inventory", "--destination", destination),
            root / "logs" / f"inventory-{arm}.log",
            600,
        )
        inventories[arm] = json.loads(destination.read_text())
    if inventories["baseline"] != inventories["evolved"]:
        raise ValueError("Cross-build upstream inventories differ")
    selected = corpus_selection(inventories["baseline"])
    write_json(root / "corpus_panel.json", selected)
    targets = {}
    for arm in ARMS:
        destination = root / "preflight" / f"corpus-targets-{arm}.json"
        run_worker(
            worker_command(
                root,
                arm,
                "targets",
                "--panel",
                root / "corpus_panel.json",
                "--destination",
                destination,
            ),
            root / "logs" / f"corpus-targets-{arm}.log",
            600,
        )
        targets[arm] = json.loads(destination.read_text())
    if targets["baseline"] != targets["evolved"]:
        raise ValueError("Cross-build upstream targets differ")
    result = []
    for row in targets["baseline"]:
        for level in LEVELS:
            reference = case_name(row, level)
            result.append(
                dict(
                    row,
                    case_id=f"{row['panel']}/{reference}",
                    reference_case_id=reference,
                    optimization_level=level,
                )
            )
    return result, {
        "url": "https://github.com/Qiskit/benchpress",
        "revision": BENCHPRESS,
        "archive_sha256": file_hash(root / "benchpress.tar"),
    }


def freeze_balanced(
    output,
    evolved,
    baseline,
    *,
    benchpress_source=None,
    seed_count=3,
    workers=8,
    trials=20,
    master_seed=20261108,
    panel_size=400,
    case_ids=(),
    timeout_s=600,
    include_supplemental=True,
):
    from .collect import (
        dependencies,
        harness_inventory,
        run_worker,
        validate_session,
        worker_command,
    )

    root = Path(output).resolve()
    sources = {"baseline": Path(baseline).resolve(), "evolved": Path(evolved).resolve()}
    if sources["baseline"] == sources["evolved"]:
        raise ValueError("Baseline and evolved must be distinct checkouts")
    if any(type(v) is not int or v < 1 for v in (seed_count, workers, trials, timeout_s)):
        raise ValueError("Seeds, workers, trials and timeout must be positive integers")
    if seed_count > 100 or panel_size != 400:
        raise ValueError("Balanced seeds must be <=100; --panel-size only applies to legacy mode")
    settings = dict(
        design="balanced",
        seed_count=seed_count,
        workers=workers,
        trials=trials,
        master_seed=master_seed,
        case_ids=sorted(set(case_ids)),
        timeout_s=timeout_s,
        include_supplemental=include_supplemental,
        optimization_levels=list(LEVELS),
        evolved_wait_time_weight=EVOLVED_WAIT_TIME_WEIGHT,
        backend_seed=BACKEND_SEED,
    )
    manifest_path = root / "manifest.json"
    if manifest_path.exists():
        manifest = json.loads(manifest_path.read_text())
        if (
            manifest["settings"] != settings
            or manifest["harness"] != harness_inventory()
            or manifest["dependencies"] != dependencies()
            or manifest["python"] != sys.version
            or manifest["host"] != host_identity()
            or any(manifest["sources"][a]["path"] != str(sources[a]) for a in ARMS)
        ):
            raise ValueError(
                "Frozen arguments, harness, dependencies, or host changed; use new output"
            )
        validate_session(root, manifest)
        return manifest
    if root.exists() and any(p.name != ".collection.lock" for p in root.iterdir()):
        raise ValueError("Nonempty output without a manifest; use a new output directory")
    for arm, source in sources.items():
        inspect_release_build(source, arm)
    root.mkdir(parents=True, exist_ok=True)
    frozen_sources = {}
    for arm, source in sources.items():
        print(f"Freezing {arm}: {source}", flush=True)
        frozen_sources[arm] = snapshot_source(root, source, arm)
    write_json(root / "builds.json", {"sources": frozen_sources})
    destination = root / "preflight/balanced-inputs.json"
    run_worker(
        worker_command(root, "baseline", "balanced-prepare", "--destination", destination),
        root / "logs/balanced-prepare.log",
        1800,
    )
    inventory = json.loads(destination.read_text())
    checked = root / "preflight/balanced-check.json"
    run_worker(
        worker_command(
            root, "evolved", "balanced-check", "--inventory", destination, "--destination", checked
        ),
        root / "logs/balanced-check.log",
        1800,
    )
    check = json.loads(checked.read_text())
    if check != inventory:
        raise ValueError("Evolved frozen fixture/target compatibility check failed")
    cases = crossed_cases(inventory, include_supplemental=include_supplemental)
    benchpress = None
    if include_supplemental:
        corpus, benchpress = _prepare_upstream(root, benchpress_source, frozen_sources)
        cases.extend(corpus)
    requested = set(case_ids)
    available = {c["case_id"] for c in cases}
    if requested - available:
        raise ValueError(f"Unknown balanced case IDs: {sorted(requested - available)}")
    if requested:
        cases = [c for c in cases if c["case_id"] in requested]
    if not cases or len({c["case_id"] for c in cases}) != len(cases):
        raise ValueError("Require nonempty unique cases")
    counts = Counter(c["panel"] for c in cases)
    for case in cases:
        seeds = seed_list(master_seed, case["case_id"], "balanced-paired")
        trigger = REGRESSION_SEEDS.get(case.get("reference_case_id"))
        if trigger is not None:
            seeds = [trigger] + [s for s in seeds if s != trigger]
        case["seeds"] = {arm: seeds[:seed_count] for arm in ARMS}
        case["weight"] = 1 / counts[case["panel"]]
        case["timeout_s"] = timeout_s
        case["configuration_digests"] = {
            arm: digest(
                {"case": case, "arm": arm, "source": frozen_sources[arm], "settings": settings}
            )
            for arm in ARMS
        }
    write_json(root / "panel.json", cases)
    artifacts = {}
    for directory in (root / "fixtures", root / "preflight", root / "sources", root / "configs"):
        for artifact in sorted(directory.rglob("*")):
            if artifact.is_file() and "__pycache__" not in artifact.parts:
                artifacts[str(artifact.relative_to(root))] = file_hash(artifact)
    for name in ("builds.json", "panel.json", "corpus_panel.json", "adapter.patch"):
        if (root / name).exists():
            artifacts[name] = file_hash(root / name)
    manifest = dict(
        format=FORMAT,
        created_at=dt.datetime.now(dt.UTC).isoformat(),
        sources=frozen_sources,
        build_policy="release_only",
        settings=settings,
        cases=cases,
        arms=list(ARMS),
        execution_mode="balanced_quality_only",
        expected_observations=2 * len(cases) * seed_count,
        benchpress=benchpress,
        selection={
            "method": "Full family × exact width × template × target × level cross product",
            "panel_case_counts": dict(counts),
            "core_inputs": 120,
            "logical_widths": WIDTHS,
            "chemistry_scope": "Supplemental coverage; no balanced chemistry core claim",
            "separate_panel_scores": True,
            "supplemental_included": include_supplemental,
        },
        seed_protocol=(
            "Same deterministic seed per case/index in both builds; triggering guard seeds retained"
        ),
        quality_protocol=(
            "One checked compilation per observation; C0/C6 for generated fixtures; "
            "upstream structural checks for corpus guards"
        ),
        heuristics={
            "baseline": "Stock decay in SABRE layout and routing",
            "evolved": "Decay + wait time, weight=0.0025, in SABRE layout and routing",
        },
        smoke_test=bool(requested),
        artifact_hashes=artifacts,
        harness=harness_inventory(),
        python=sys.version,
        dependencies=dependencies(),
        host=host_identity(),
        thread_environment=THREAD_ENV,
    )
    manifest["session_id"] = digest(manifest)
    for relative, expected in manifest["harness"].items():
        target = root / "harness" / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / relative, target)
        if file_hash(target) != expected:
            raise ValueError("Harness changed while freezing; use a new output")
    write_json(manifest_path, manifest)
    return manifest
