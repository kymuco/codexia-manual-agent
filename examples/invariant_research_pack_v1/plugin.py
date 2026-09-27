from __future__ import annotations

from invariant.spec import BasePlugin

from codexia_manual_agent.workflow_runtime.research_v1 import (
    ResearchCompletionCriterionV1,
    ResearchWorkflowImplementationV1,
    research_pack_binding,
    research_role_bindings,
    research_workflow_binding,
)


class ResearchPackProviderV1(BasePlugin):
    def initialize(self, **deps) -> None:
        pass

    def shutdown(self) -> None:
        pass

    def codexia_pack_distribution(self):
        workflow = research_workflow_binding()
        roles = research_role_bindings()
        return {
            "schema_version": 1,
            "pack": research_pack_binding().to_dict(),
            "workflows": [workflow.to_dict()],
            "roles": [role.to_dict() for role in roles],
            "capabilities": [],
        }

    def codexia_workflow_implementation(self, workflow_binding):
        expected = research_workflow_binding()
        if workflow_binding != expected.to_dict():
            raise ValueError("Unsupported or non-exact WorkflowBinding")
        implementation = ResearchWorkflowImplementationV1()
        if implementation.binding != expected:
            raise RuntimeError(
                "Research workflow implementation drifted from distribution"
            )
        return implementation

    def codexia_completion_criterion(self, workflow_binding):
        expected = research_workflow_binding()
        if workflow_binding != expected.to_dict():
            raise ValueError("Unsupported or non-exact WorkflowBinding")
        criterion = ResearchCompletionCriterionV1()
        if criterion.binding != expected:
            raise RuntimeError(
                "Research completion criterion drifted from distribution"
            )
        return criterion
