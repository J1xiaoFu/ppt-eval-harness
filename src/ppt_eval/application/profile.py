"""Compile a versioned profile into an immutable, deterministic execution DAG."""

from __future__ import annotations

from typing import Mapping, Sequence

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
        configured_pipeline = profile.metadata.get("pipeline_nodes", ())
        if configured_pipeline:
            nodes = self._pipeline_nodes(configured_pipeline, baseline)
            dag = EvaluationDag(nodes=(baseline, *nodes))
            self.assert_invariants(dag)
            return dag

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

    @staticmethod
    def _pipeline_nodes(
        value: object,
        baseline: DagNode,
    ) -> tuple[DagNode, ...]:
        if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
            raise ValueError("profile metadata.pipeline_nodes must be a sequence")
        nodes: list[DagNode] = []
        seen = {baseline.node_id}
        for raw in value:
            if not isinstance(raw, Mapping):
                raise ValueError("each pipeline node must be a mapping")
            node_id = str(raw.get("node_id") or "").strip()
            oracle_id = str(raw.get("oracle_id") or "").strip()
            if not node_id or not oracle_id:
                raise ValueError("pipeline node_id and oracle_id must not be blank")
            if node_id in seen:
                raise ValueError(f"duplicate pipeline node_id {node_id!r}")
            dependencies = tuple(
                str(item).strip()
                for item in raw.get("dependencies", ())
                if str(item).strip()
            )
            if not dependencies:
                dependencies = (baseline.node_id,)
            nodes.append(
                DagNode(
                    node_id=node_id,
                    oracle_id=oracle_id,
                    dependencies=dependencies,
                    kind=DagNodeKind(str(raw.get("kind") or "OBSERVE").upper()),
                    mandatory=bool(raw.get("mandatory", False)),
                )
            )
            seen.add(node_id)
        return tuple(nodes)

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
        dependencies = {node.node_id: set(node.dependencies) for node in dag.nodes}
        for node in dag.nodes:
            if node is baseline:
                continue
            frontier = list(dependencies[node.node_id])
            visited: set[str] = set()
            while frontier:
                dependency = frontier.pop()
                if dependency in visited:
                    continue
                visited.add(dependency)
                frontier.extend(dependencies.get(dependency, ()))
            if baseline.node_id not in visited:
                raise ValueError(
                    f"pipeline node {node.node_id!r} must transitively depend on baseline"
                )
