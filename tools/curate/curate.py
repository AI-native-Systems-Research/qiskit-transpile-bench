"""One-time curation from an archived Qiskit benchmark tree, never from a candidate.

Run: python tools/curate/curate.py --source /path/to/archived/0131cbbcc
Only imports Qiskit from the pinned verifier environment. Upstream files lacking
redistribution provenance are replaced by declared upstream synthesis constructions
as permitted by implementation-plan section 3.8.
"""

import argparse
import copy
import importlib.util
import json
import random
import shutil
import tempfile
from pathlib import Path

import numpy as np
import qiskit
from qiskit import QuantumCircuit
from qiskit.quantum_info import random_unitary
from qiskit.transpiler import CouplingMap, Target

from qtb.canonical import digest, write_circuit, write_json
from qtb.evaluator.statistics import hierarchical_weights
from qtb_worker.adapter import export_circuit, export_target, import_circuit

ROOT = Path(__file__).resolve().parents[2]
ITERATIONS = ["qft_n100", "square_heisenberg_n100", "qaoa_ba_n100_3reps"]
RESTRICTED = {
    "hwb12",
    "revlib_4gt10_v1_81",
    "revlib_4mod5_v0_19",
    "revlib_mod8_10_178",
    "revlib_cnt3_5_179",
    "revlib_cnt3_5_180",
}
REPLACEMENTS = {
    "revlib_4gt10_v1_81": ("mcx_kg24_n5", "ft_mcx_5", "utils.mcx_circuit"),
    "revlib_mod8_10_178": ("mcx_kg24_n6", "ft_mcx_6", "utils.mcx_circuit"),
    "revlib_cnt3_5_179": (
        "adder_modular_v17_n8",
        "ft_modular_adder_8",
        "utils.modular_adder_circuit",
    ),
    "revlib_cnt3_5_180": (
        "adder_modular_v17_n16",
        "ft_modular_adder_16",
        "utils.modular_adder_circuit",
    ),
    "revlib_4mod5_v0_19": (
        "adder_modular_v17_n6",
        "ft_modular_adder_6",
        "utils.modular_adder_circuit",
    ),
    "hwb12": ("multiplier_h18_n20", "ft_multiplier_20", "utils.multiplier_circuit"),
}
OPTIONS = dict(
    approximation_degree=1.0,
    qubits_initially_zero=True,
    initial_layout=None,
    layout_method=None,
    routing_method=None,
    translation_method=None,
    scheduling_method=None,
)


def load_probe(source):
    spec = importlib.util.spec_from_file_location(
        "curation_probe", ROOT / "tools/probes/confirm_probe.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.configure_source(str(source))
    return module


# Version 7 (design/updated_confirm_plan.md). Confirm-timing keeps one large end-to-end case
# per family; the companion panel times the scored level-2 case of four cheaper families.
CONFIRM_TIMING = [
    ("qft_n100", 3),
    ("square_heisenberg_n100", 0),
    ("qaoa_ba_n100_3reps", 3),
    ("qv_n50_d50", 2),
    ("multiplier_h18_n32", 0),
    ("bv_all_ones_n100", 3),
    ("queko_bss_53", 2),
    ("su2_circular_n89", 0),
]
COMPANION_GROUPS = [
    "square_heisenberg_n100",
    "multiplier_h18_n32",
    "bv_all_ones_n100",
    "queko_bss_53",
]
# Quality batches of about this many seconds of cluster work (plan section 4.1, Q3).
TARGET_BATCH_S = 60
DEFAULT_SEED_S = 0.5


def seed_seconds(row, level):
    """Estimated cluster seconds per quality seed from a catalog row's baseline probe.

    The section 4.3 model without the two routing-prefix compiles that Q1 removed: a fixed
    0.36 s, 1.4e-4 s per N2 gate for the checks, and 1.25 times the M1 compile time.
    """
    probe = (row.get("baseline_probe") or {}).get(str(level))
    if not probe:
        return None
    n2 = sum(probe["N2"]) / len(probe["N2"])
    return round(0.36 + 1.4e-4 * n2 + 1.25 * probe["t"], 3)


def quality_batch_schedule(cases, catalog):
    """The frozen per-case batch schedule: estimated seconds per seed of each quality case.

    Cases without a probe row (the generated replacements) use ``DEFAULT_SEED_S``. The cx
    and ecr twins use their cz row, which compiles the same circuit.
    """
    rows = {}
    for row in catalog:
        if row["input_group"] in REPLACEMENTS or row.get("replaces"):
            continue  # the probe timed the replaced circuit, not its replacement
        rows[row["input_group"], row["target_id"], row["probe_id"].endswith("_sabre")] = row
    estimates = {}
    for case in cases:
        if case["role"] in {"timing", "memory"}:
            continue
        target = case["target"]["file"].split("/")[-1].removesuffix(".target.json")
        sabre = case["variant"] == "sabre_methods"
        row = rows.get((case["input_group"], target, sabre)) or rows.get(
            (case["input_group"], target.replace("_cx", "_cz").replace("_ecr", "_cz"), sabre)
        )
        value = seed_seconds(row or {}, case["optimization_level"])
        if value is not None and value != DEFAULT_SEED_S:
            estimates[case["case_id"]] = value
    return {
        "version": 1,
        "target_batch_s": TARGET_BATCH_S,
        "default_seed_s": DEFAULT_SEED_S,
        "seed_s": dict(sorted(estimates.items())),
    }


def policy(profile, cases=(), catalog=()):
    prefix = "CA" if profile == "confirm-profile" else "IA"
    required = [
        "harness/roundtrip",
        "baseline/preflight",
        "audit/determinism",
        f"{prefix}1/C0",
        f"{prefix}1/C1-C5",
        f"{prefix}1/C6",
        f"{prefix}1/C7",
        f"{prefix}1/stage-coverage",
        f"{prefix}2/improvement",
        f"{prefix}5/timing",
        f"{prefix}6/completeness",
    ]
    if prefix == "CA":
        required += ["CA3/breadth", "CA5/confirm-timing", "CA5/memory"]
    confirm = prefix == "CA"
    extra_protocol = {}
    if confirm:
        extra_protocol = dict(
            quality_batch_schedule=quality_batch_schedule(cases, catalog),
            companion_groups=COMPANION_GROUPS,
        )
    return dict(
        format="qtb-policy/1",
        profile=profile,
        version=7,
        # C7: seeds per variant and mode, and whether full-pipeline outputs are checked too.
        clifford_seeds=5 if confirm else 3,
        clifford_full_mode=confirm,
        quality_cap_rule="plain",
        required_ids=required,
        quality_prerequisites=["harness/roundtrip", "audit/determinism"],
        practical_ratio=0.99 if prefix == "CA" else 1.0,
        quality_cap=1.05,
        improvement_multiplier=2.0,
        guard_multiplier=3.0,
        rng_seed=20260924,
        bootstrap_replicates=10000,
        iterations_groups=ITERATIONS,
        measurement_protocol=dict(
            quality_batch_size=25,
            # Timing: a 4-round screen ends a clearly clean panel early; the full
            # count is 6 rounds in the iterations loop and 10 in confirm.
            screen_rounds=4,
            timing_rounds=10 if prefix == "CA" else 6,
            warmups=1,
            minimum_calls=2,
            minimum_ns=1000000000,
            companion_seeds=list(range(10 if confirm else 20)),
            companion_rounds=3,
            memory_processes=3 if confirm else 5,
            rerun_multiplier=2,
            audit_fraction=0.05,
            audit_minimum=10,
            output_retention_bytes=8 * 1024 * 1024,
            # A candidate timeout fails only when repeated, with the baseline seed well
            # inside the timeout and a quiet host; otherwise it is unresolved.
            quality_timeout=dict(baseline_fraction=0.5, max_load_per_cpu=1.0),
            **extra_protocol,
        ),
        # Fixed cost guards; there is no per-machine calibration. See
        # design/remove-calibration-plan.md for how to sanity-check them on a runner.
        cost_thresholds=dict(
            panel_ratio=1.10,
            case_ratio=1.20,
            case_floor_ns=25_000_000,
            case_floor_bytes=32 * 1024 * 1024,
            screen_fraction=0.5,
        ),
        tolerances=dict(operator_rtol=1e-7, operator_atol=1e-8, state_atol=1e-8),
        upstream_test_budgets_s=dict(python=14400, rust=10800),
    )


def curate(source, root=ROOT):
    if qiskit.__version__ != "2.5.2":
        raise RuntimeError("Curate only in the pinned Qiskit 2.5.2 verifier environment")
    probe = load_probe(source)
    catalog = json.loads((ROOT / "tools/curate/catalog.json").read_text())
    for row in catalog:
        old = row["input_group"]
        if old in REPLACEMENTS:
            name, recipe, source_name = REPLACEMENTS[old]
            row.update(
                input_group=name,
                circuit_recipe=recipe,
                source="test/benchmarks/" + source_name,
                provenance_grade="C",
                replaces=old,
                baseline_probe={},
            )
    fixtures = root / "fixtures"
    for d in ("circuits", "targets", "references"):
        (fixtures / d).mkdir(parents=True, exist_ok=True)
    targets, recipes, circuits, artifacts, references = {}, {}, {}, {}, {}
    provenance = []
    target_names = {r["target_recipe"]: r["target_id"] for r in catalog}
    target_names.update(hh9_cx="heavy_hex_d9_cx", hh9_ecr="heavy_hex_d9_ecr")
    for recipe, name in sorted(target_names.items()):
        kwargs, native = probe.build_target(recipe)
        if "target" in kwargs:
            target = kwargs["target"]
            constraints = {"constraint_form": "target"}
        else:
            coupling = kwargs["coupling_map"]
            edges = [
                list(e)
                for e in (coupling.get_edges() if isinstance(coupling, CouplingMap) else coupling)
            ]
            width = max(q for edge in edges for q in edge) + 1
            target = Target.from_configuration(
                kwargs["basis_gates"], width, coupling_map=CouplingMap(edges)
            )
            constraints = dict(
                constraint_form="loose", basis_gates=kwargs["basis_gates"], coupling_map=edges
            )
        data = export_target(target, native)
        file = f"targets/{name}.target.json"
        write_json(fixtures / file, data)
        targets[name] = dict(file=file, sha256=digest(data), native_2q_names=native)
        recipes[name] = constraints

    def persist(name, circuit, folder="circuits", restricted=False):
        data = export_circuit(circuit)
        # Validate the typed input importer before freezing its bytes.
        if export_circuit(import_circuit(data)) != data:
            raise RuntimeError(f"Curation roundtrip failed: {name}")
        file = f"{folder}/{name}.ops.jsonl.gz"
        if restricted:
            with tempfile.TemporaryDirectory() as directory:
                hash_ = write_circuit(
                    Path(directory) / "input.gz", data["header"], data["operations"]
                )
        else:
            hash_ = write_circuit(fixtures / file, data["header"], data["operations"])
        return dict(file=file, sha256=hash_)

    from test.benchmarks import utils as upstream

    original_random_unitary = upstream.random_unitary
    for row in catalog:
        name = row["input_group"]
        if name in circuits:
            continue
        print("curate", name, flush=True)
        matrix_seed = (
            10
            if row["circuit_recipe"].startswith("qv_w")
            else (12345 if name == "qv_n50_d50" else 0)
        )
        generator = np.random.default_rng(matrix_seed)
        # Upstream seeds pairings but omits matrix seeds. Preserve the constructor,
        # inject its matrix RNG explicitly, then freeze the resulting artifact.
        upstream.random_unitary = lambda dim, rng=generator: random_unitary(dim, seed=rng)
        try:
            circuit = probe.build_circuit(row["circuit_recipe"])
        finally:
            upstream.random_unitary = original_random_unitary
        circuits[name] = circuit
        artifacts[name] = persist(name, circuit, restricted=name in RESTRICTED)
        if "trotter" in name:
            reference = circuit.decompose(reps=10)
            references[name] = dict(
                kind="frozen_circuit",
                contract="product_formula",
                **persist(name, reference, folder="references"),
            )
        provenance.append(
            dict(
                input_group=name,
                source=row["source"],
                commit="0131cbbcc",
                qiskit=qiskit.__version__,
                artifact=artifacts[name],
                license="unresolved; artifact omitted"
                if name in RESTRICTED
                else (
                    "BSD-3-Clause (QUEKO) and Apache-2.0 (Qiskit adapter)"
                    if name.startswith("queko")
                    else "Apache-2.0"
                ),
                matrix_seed=matrix_seed if name.startswith("qv_") else None,
                replaces=row.get("replaces"),
            )
        )
    for name in ("single_h", "cancel_2q"):
        circuit = QuantumCircuit(1 if name == "single_h" else 2)
        circuit.h(0)
        if name == "cancel_2q":
            circuit.h(0)
            for _ in range(4):
                circuit.cx(0, 1)
        circuits[name] = circuit
        artifacts[name] = persist(name, circuit)
        provenance.append(
            dict(
                input_group=name,
                source="test/benchmarks/transpiler_benchmarks.py",
                commit="0131cbbcc",
                qiskit=qiskit.__version__,
                artifact=artifacts[name],
                license="Apache-2.0",
                matrix_seed=None,
                replaces=None,
            )
        )
    rng = random.Random(20260924)
    variants = {}
    for name in ITERATIONS:
        data = export_circuit(circuits[name])
        for op in data["operations"]:
            if op[0] in {"rx", "ry", "rz"}:
                op[3] = [float(rng.choice([-3, -1, 1, 3]) * np.pi / 2).hex()]
        variants[name] = persist(name + "_cv1", import_circuit(data))

    def make_case(row, level, role, prefix="confirm"):
        name, target_id = row["input_group"], row["target_id"]
        symbolic = bool(circuits[name].parameters)
        variant = (
            "sabre_methods"
            if row["probe_id"].endswith("_sabre")
            else "default_methods"
            if name.startswith("queko")
            else "symbolic"
            if symbolic
            else "numeric"
        )
        options = dict(OPTIONS)
        if variant == "sabre_methods":
            options.update(layout_method="sabre", routing_method="sabre")
        active = len(
            {
                circuits[name].find_bit(q).index
                for i in circuits[name].data
                if i.operation.name != "barrier"
                for q in i.qubits
            }
        )
        suffix = "+sabre" if variant == "sabre_methods" else ""
        case = dict(
            case_id=f"{prefix}/{name}{suffix}/{target_id}/L{level}",
            role=role,
            family=row["family"],
            input_group=name,
            variant=variant,
            size_band="small" if active <= 16 else "medium" if active <= 64 else "large",
            logical_qubits=circuits[name].num_qubits,
            active_qubits=active,
            topology=row["topology"],
            native_basis=row["native_basis"],
            optimization_level=level,
            circuit=artifacts[name],
            target=targets[target_id],
            options=options,
            semantic_reference=references.get(name, {"kind": "input"}),
            input_domain="all_zero",
            seeds_per_block=row["seeds"].get(str(level), 100),
            weight=0.0,
            timeout_s=120,
            modes=["quality", "prefix"],
            provenance=dict(
                grade=row["provenance_grade"], source=row["source"], commit="0131cbbcc"
            ),
            **recipes[target_id],
        )
        if role == "canary":
            expected = 300 if name == "su2_circular_n100" else 3
            case["expected"] = dict(D2=expected, N2=expected)
        if name in variants:
            case["clifford_variant"] = variants[name]
        return case

    confirm = [
        make_case(row, int(level), role) for row in catalog for level, role in row["roles"].items()
    ]
    # Level 3 VF2 odd-ring cost guard is deliberately limited to ten seeds.
    iteration = []
    for name in ITERATIONS:
        row = next(r for r in catalog if r["input_group"] == name)
        for basis in ("cz", "cx", "ecr"):
            r = dict(row, target_id="heavy_hex_d9_" + basis, native_basis=basis)
            case = make_case(r, 2, "scored" if basis == "cz" else "guard", "iterations")
            case["panel"] = "primary" if basis == "cz" else "basis-" + basis
            case["weight"] = 1 / 3
            iteration.append(case)
            if basis != "cz":
                confirm.append(
                    dict(
                        case, case_id=case["case_id"].replace("iterations/", "confirm/"), weight=0.0
                    )
                )
    for name, levels in [("su2_circular_n100", [2]), ("long_2q_sequence", [2, 3])]:
        row = next(r for r in catalog if r["input_group"] == name)
        iteration.extend(make_case(row, level, "canary", "iterations") for level in levels)
    timing = []

    def timed(case, id_, seed, mode="timing_e2e", panel="timing"):
        return dict(
            copy.deepcopy(case),
            case_id=id_,
            role="timing",
            panel=panel,
            weight=0.0,
            modes=[mode],
            timing={"fixed_seed": seed},
            seeds_per_block=1,
        )

    template = next(c for c in confirm if c["input_group"] == "qaoa_complete_n8")
    for i, name in enumerate(("single_h", "cancel_2q"), 1):
        c = dict(
            template,
            input_group=name,
            circuit=artifacts[name],
            logical_qubits=i,
            active_qubits=i,
            optimization_level=None,
            target=targets["mumbai_27_loose"],
            semantic_reference={"kind": "input"},
            family="timing_microbenchmark",
            provenance={
                "commit": "0131cbbcc",
                "grade": "A",
                "source": "test/benchmarks/transpiler_benchmarks.py",
            },
            **recipes["mumbai_27_loose"],
        )
        timing.append(timed(c, f"T{i}", 20220125))
    for name in ("qv_n14_d14", "long_2q_sequence"):
        template = next(c for c in confirm if c["input_group"] == name)
        for level in range(4):
            timing.append(timed(dict(template, optimization_level=level), f"T{len(timing) + 1}", 0))
    for case in iteration[:9]:
        # The cx/ecr twins share the cz case's layout and routing; they are timed
        # only when the change can reach translation or optimization.
        panel = "timing" if case["native_basis"] == "cz" else "timing-basis"
        timing.append(timed(case, f"T{len(timing) + 1}", 1234567845, "timing_reuse", panel))
    for basis in ("cx", "cz", "ecr"):
        case = next(c for c in iteration if c["native_basis"] == basis)
        timing.append(timed(case, "preset/" + basis, 0, "preset_build", "preset"))
    for profile, cases in [("iterations-profile", iteration), ("confirm-profile", confirm)]:
        cases.extend(copy.deepcopy(timing))
        if profile == "confirm-profile":
            heavy = [
                "qft_n100",
                "square_heisenberg_n100",
                "qaoa_ba_n100_3reps",
                "qv_n50_d50",
                "multiplier_h18_n32",
                "bv_all_ones_n100",
                "queko_bss_53",
                "su2_circular_n89",
            ]
            # One large end-to-end case per family (version 7; version 6 timed 20).
            for name, level in CONFIRM_TIMING:
                template = next(
                    c for c in cases if c["input_group"] == name and c["role"] == "scored"
                )
                cases.append(
                    timed(
                        dict(template, optimization_level=level),
                        f"confirm-timing/{name}/L{level}",
                        0,
                        panel="confirm-timing",
                    )
                )
            for name in heavy + ["multiplier_h18_n20"]:
                template = next(
                    c for c in cases if c["input_group"] == name and c["optimization_level"] == 2
                )
                cases.append(
                    dict(
                        copy.deepcopy(template),
                        case_id="memory/" + name,
                        role="memory",
                        panel="memory",
                        weight=0.0,
                        modes=["memory"],
                    )
                )
        weights = hierarchical_weights([c for c in cases if c["role"] == "scored"])
        for c in cases:
            if c["role"] == "scored":
                c["weight"] = weights[c["case_id"]]
        manifest = dict(
            format="qtb-manifest/1",
            profile=profile,
            version=7,
            cases=cases,
            coverage_gaps=[
                "No line target in upstream suite",
                "G2, G3, G6 have one sparse topology class",
                "G7 has no scored level 3",
                "Some family/size cells have fewer than three independent groups",
                "G4, G5, G8 have no large-band scored inputs",
            ],
            unavailable_fixtures=[],
            replacements={k: v[0] for k, v in REPLACEMENTS.items()}
            if profile == "confirm-profile"
            else {},
        )
        directory = root / "profiles" / profile
        directory.mkdir(parents=True, exist_ok=True)
        write_json(directory / "manifest.json", manifest)
        write_json(directory / "policy.json", policy(profile, cases, catalog))
    write_json(fixtures / "provenance.json", provenance)
    shutil.copy2(source / "LICENSE.txt", fixtures / "LICENSE-QISKIT.txt")
    lines = [
        "# Fixture provenance",
        "",
        "Curated from Qiskit commit `0131cbbcc` using the pinned Qiskit 2.5.2 importer.",
        "Canonical data is the contract. Comparison runs never call these constructors.",
        "Qiskit-derived artifacts retain the Apache-2.0 license in LICENSE-QISKIT.txt.",
        "QUEKO source: https://github.com/UCLA-VAST/QUEKO-benchmark (BSD-3-Clause).",
        "The six RevLib artifacts with unresolved terms were replaced under plan section 3.8.",
        "Each replacement is a distinct upstream synthesis-library construction; "
        "the manifest records the mapping.",
        "Baseline roles, cost, and oracle coverage are validated on each new runner "
        "with A/A and known-outcome runs.",
        "",
        "| Fixture | Upstream source | License / availability |",
        "| --- | --- | --- |",
    ]
    lines += [f"| {p['input_group']} | {p['source']} | {p['license']} |" for p in provenance]
    (fixtures / "PROVENANCE.md").write_text("\n".join(lines) + "\n")


def reprofile(root=ROOT):
    """Apply the version-7 profile changes to the shipped version-6 profiles.

    For a checkout without the archived Qiskit tree: the fixtures are unchanged, so the
    cases ``curate`` would produce differ from version 6 only by these edits. It rewrites
    both profiles' policies and applies to the confirm manifest the role and seed changes
    of ``catalog.json`` (W2), the one-per-family confirm-timing panel and the recomputed
    scored weights. It refuses a manifest that is not version 6 or 7.
    """
    catalog = json.loads((ROOT / "tools/curate/catalog.json").read_text())
    for profile in ("iterations-profile", "confirm-profile"):
        directory = root / "profiles" / profile
        manifest = json.loads((directory / "manifest.json").read_text())
        if manifest["version"] not in {6, 7}:
            raise RuntimeError(f"{profile} is version {manifest['version']}, not 6 or 7")
        cases = manifest["cases"]
        if profile == "confirm-profile":
            names = {
                row["input_group"]: REPLACEMENTS.get(row["input_group"], (row["input_group"],))[0]
                for row in catalog
            }
            plan = {}
            for row in catalog:
                sabre = row["probe_id"].endswith("_sabre")
                for level, role in row["roles"].items():
                    plan[names[row["input_group"]], row["target_id"], sabre, int(level)] = (
                        role,
                        row["seeds"].get(level, 100),
                    )
            for case in cases:
                if case["case_id"].startswith("confirm/") and case["role"] not in {
                    "timing", "memory"
                }:
                    target = case["target"]["file"].split("/")[-1].removesuffix(".target.json")
                    sabre = case["variant"] == "sabre_methods"
                    key = case["input_group"], target, sabre, case["optimization_level"]
                    if key in plan:
                        case["role"], case["seeds_per_block"] = plan[key]
            timing = {f"confirm-timing/{name}/L{level}" for name, level in CONFIRM_TIMING}
            cases[:] = [
                c
                for c in cases
                if c.get("panel") != "confirm-timing" or c["case_id"] in timing
            ]
            missing = timing - {c["case_id"] for c in cases}
            if missing:
                raise RuntimeError(f"Confirm-timing cases missing: {sorted(missing)}")
            for case in cases:
                if case["role"] == "memory":
                    # curate copies a memory case from the group's first level-2 case.
                    template = next(
                        c
                        for c in cases
                        if c["input_group"] == case["input_group"]
                        and c["optimization_level"] == 2
                        and c["role"] not in {"timing", "memory"}
                    )
                    case["seeds_per_block"] = template["seeds_per_block"]
                if case["role"] != "scored":
                    case["weight"] = 0.0
            weights = hierarchical_weights([c for c in cases if c["role"] == "scored"])
            for case in cases:
                if case["role"] == "scored":
                    case["weight"] = weights[case["case_id"]]
        manifest["version"] = 7
        write_json(directory / "manifest.json", manifest)
        write_json(directory / "policy.json", policy(profile, cases, catalog))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path)
    parser.add_argument("--output", type=Path, default=ROOT)
    parser.add_argument(
        "--reprofile",
        action="store_true",
        help="apply the version-7 changes to the shipped profiles (no source tree needed)",
    )
    args = parser.parse_args()
    if args.reprofile:
        reprofile(args.output.resolve())
    elif args.source is None:
        parser.error("--source is required unless --reprofile")
    else:
        curate(args.source.resolve(), args.output.resolve())
