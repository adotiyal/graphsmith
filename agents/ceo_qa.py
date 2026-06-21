"""
CEO Q&A Node
------------
This node fires when any agent has questions for the CEO.
interrupt_before=["ceo_qa"] pauses the graph before this node runs.
main.py reads ceo_qa_pending, gets CEO's answer, injects it via
graph.update_state({"ceo_qa_answer": answer}), then resumes.

This node's only job: move the answer from ceo_qa_answer into qa_log
and clear the interrupt state so routing can send back to the asking agent.

DESIGN DECISION: ceo_qa_from is NOT cleared here — the return routing
function reads it to know where to go. The receiving agent clears it
after it completes its main work.
"""

from graph.state import ProjectState


def run(state: ProjectState) -> dict:
    answer = state.get("ceo_qa_answer") or "(no answer provided)"
    from_agent = state.get("ceo_qa_from") or "unknown"
    qa_log = list(state.get("qa_log") or [])

    # Find the unanswered CEO question entry from this agent and fill in the answer
    for entry in reversed(qa_log):
        if entry.get("from") == from_agent and entry.get("to") == "ceo" and "answer" not in entry:
            entry["answer"] = answer
            break

    return {
        "current_node": "ceo_qa",
        "qa_log": qa_log,
        "ceo_qa_answer": None,
        "ceo_qa_pending": None,
        # ceo_qa_from intentionally kept — routing needs it to return to the right agent
    }
