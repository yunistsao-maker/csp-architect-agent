"""COE document agent (AgentCore Runtime entrypoint). Templates: templates/coe/. Logic: doc_agent.py.

    agentcore configure -e coe_agent.py -n coe_doc_agent -r us-east-1 -rf requirements.txt \
      -dt direct_code_deploy -rt PYTHON_3_12 -dm -ni      # then VPC network mode, see docs/portal-agents.md
"""

from doc_agent import build_portal_app

portal = build_portal_app("coe")
app = portal.app


@app.entrypoint
def invoke(payload: dict, context=None) -> dict:
    return portal.invoke(payload, context)


if __name__ == "__main__":
    app.run()
