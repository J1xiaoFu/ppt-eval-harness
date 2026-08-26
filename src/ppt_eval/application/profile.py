"""Compile a versioned profile into an immutable, deterministic execution DAG."""

from __future__ import annotations

from ppt_eval.domain import DagNode, DagNodeKind, EvalProfile, EvaluationDag

from .oracle import BaselinePptQualityOracle


class ProfileCompiler:
    BASELINE_NODE_ID = "baseline_ppt_quality"
    BASELINE_ALIASES = frozenset(("baseline_ppt_quality", "baseline.ppt_quality"))

    def compile(self, profile: EvalProfile) -> EvaluationDag:
        """Build the DAG and inject the mandatory baseline unconditionally."""

        baseline = DagNode(
            node_id=self.BASELINE_NODE_ID,
            oracle_id=BaselinePptQualityOracle.ORACLE_ID,
            kind=DagNodeKind.BASELINE,
            mandatory=True,
        )
        seen = set(self.BASELINE_ALIASES)
        scene_nodes: list[DagNode] = []
        for oracle_id in profile.enabled_oracle_ids:
            if oracle_id in seen:
                continue
            seen.add(oracle_id)
            scene_nodes.append(
                DagNode(
                    node_id=f"scene:{oracle_id}",
                    oracle_id=oracle_id,
                    dependencies=(baseline.node_id,),
                    kind=DagNodeKind.SCENE,
                    mandatory=False,
                )
            )
        dag = EvaluationDag(nodes=(baseline, *scene_nodes))
        self.assert_invariants(dag)
        return dag

    @classmethod
    def assert_invariants(cls, dag: EvaluationDag) -> None:
        baseline_nodes = [
            node
            for node in dag.nodes
            if node.oracle_id == BaselinePptQualityOracle.ORACLE_ID
        ]
        if len(baseline_nodes) != 1:
            raise ValueError("DAG must contain exactly one baseline PPT quality Oracle")
        baseline = baseline_nodes[0]
        if not baseline.mandatory or baseline.kind != DagNodeKind.BASELINE:
            raise ValueError("baseline PPT quality Oracle must be mandatory")
        for node in dag.nodes:
            if node is not baseline and baseline.node_id not in node.dependencies:
                raise ValueError(
                    f"scene node {node.node_id!r} must depend on the baseline node"
                )
