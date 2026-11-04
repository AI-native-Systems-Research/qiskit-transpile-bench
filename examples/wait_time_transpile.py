"""Transpile with wait-time scoring in both SABRE layout search and routing.

Requires the qiskit-evolved fork, including its built native extension and
SabreLayout(heuristic=...) API. From the benchmark repository root, run:

    PYTHONPATH=../qiskit-evolved .venv/bin/python examples/wait_time_transpile.py
"""

from qiskit import QuantumCircuit
from qiskit._accelerate.sabre import Heuristic, SetScaling
from qiskit.transpiler import CouplingMap, PassManager
from qiskit.transpiler.passes import SabreLayout
from qiskit.transpiler.preset_passmanagers import generate_preset_pass_manager

# A star of interactions needs routing on a device with line connectivity.
circuit = QuantumCircuit(5)
circuit.h(0)
for qubit in (4, 2, 1, 3):
    circuit.cx(0, qubit)

coupling_map = CouplingMap.from_line(5, bidirectional=True)
width = coupling_map.size()
seed = 42

# Match the benchmark's complete wait_time preset: decay plus the wait-time term.
heuristic = (
    Heuristic(attempt_limit=10 * width)
    .with_basic(1.0, SetScaling.Constant)
    .with_lookahead([0.5 / width], SetScaling.Constant)
    .with_decay(0.001, 5)
    .with_wait_time(2.2e-5)
)

pm = generate_preset_pass_manager(
    optimization_level=2,
    coupling_map=coupling_map,
    basis_gates=["rz", "sx", "x", "cx"],
    seed_transpiler=seed,
)
pm.layout = PassManager(
    SabreLayout(
        coupling_map,
        heuristic=heuristic,
        seed=seed,
        layout_trials=4,
        swap_trials=4,
        skip_routing=False,  # Apply both the layout and integrated routing.
    )
)
pm.routing = None  # SabreLayout has already routed using the same heuristic.

# The remaining preset stages translate SWAPs to native gates and optimize.
compiled = pm.run(circuit)
print("Initial logical-to-physical mapping:", compiled.layout.initial_index_layout())
print("Final logical-to-physical mapping:", compiled.layout.final_index_layout())
print("Native gate counts:", dict(compiled.count_ops()))
print("Native two-qubit depth:", compiled.depth(lambda inst: inst.operation.num_qubits == 2))
print(compiled.draw(output="text"))
