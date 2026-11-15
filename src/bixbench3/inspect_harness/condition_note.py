"""v5 dag-orchestrator: append the orchestration note to the end of the task prompt.

The original task prompt (paper context, data, required outputs) is left untouched; this
note only adds a way of organizing the work with the `run_subagent` tool. It contains no
ground truth, reference outputs or grader-derived values, and it states that the task's own
output requirements take precedence.
"""

from __future__ import annotations

from inspect_ai.solver import Generate, Solver, TaskState, solver

CONDITION = "v5-dag-orchestrator"

NOTE = """## Experiment Note: Work as an Orchestrator of Sub-Agents

Everything above (data, methods, required outputs, file names and formats, time limit) still applies
and takes precedence over this note. This note only changes how you organize the work.

You are the ORCHESTRATOR. Do not compute artifacts yourself: plan, delegate with `run_subagent` /
`run_subagents`, verify and store. A sub-agent starts with an empty context; its brief is all it
knows, so every brief must be self-contained.

### Step 1: dependency plan
Write the plan yourself: list every required output artifact with its id, exact specification
(quoted from the task), required inputs (raw data, shared reference files and/or other artifacts),
expected output file, and level (level 1 = needs no other artifact; otherwise 1 + the highest level
among the artifacts it needs). Check that the graph has no cycle and covers every output the task
requires, then save it as /workspace/work/pipeline/plan.json. Keep it in that file and re-read it
when needed rather than repeating it in your messages.
Safeguard: if the plan would have more than about 30 artifacts, or the task text is too long for you
to analyze reliably, delegate the planning to one sub-agent with role "planner" instead (give it the
full task text and the list of files under /data) and save its plan as plan.json.

### Step 2: build level by level
Artifacts of the same level never depend on each other. For level = 1, 2, ...: start ALL artifacts
of that level at once with one `run_subagents` call (one job per artifact, role "artifact:<id>";
use `run_subagent` when a level has a single artifact). Start the next level only after every
artifact of the current level is verified. Each brief states:
- GOAL: the artifact's exact specification, quoted from the task;
- ALLOWED INPUTS: the shared reference files (raw data, reference genome, annotation/GTF, sample
  metadata under /data: always allowed) and the stored outputs of the lower-level artifacts this
  artifact depends on, with their exact paths;
- FORBIDDEN: outputs of the other artifacts of the same level, outputs of lower-level artifacts it
  does not depend on, and any expected value, reference result or published number used as a target;
- OUTPUT: /workspace/work/pipeline/artifacts/<level>_<id>/, holding the final file and a
  manifest.json (inputs actually read, scripts, status, known issues);
- the ARTIFACT BUILDER RULES below, copied into the brief.
When a level returns, verify each artifact yourself with short commands (`ls -l`, `head`, `wc -l`):
the file exists, is non-empty, has the required format, and manifest.json lists only allowed inputs.
Rebuild an artifact only if it fails this check: then call a new sub-agent once with the same brief
plus the error. Never re-run an artifact that passes verification. Update plan.json after each level.

### Artifact builder rules (copy them into every artifact brief)
1. First break the artifact into ordered elementary steps (one operation each, with its inputs,
   output and a success check) and save them as bricks_plan.json in your output folder.
2. Then call one sub-agent per step, in order, with role "brick:<k>-<name>". Each brick brief repeats
   the ALLOWED and FORBIDDEN inputs, adds the outputs of the earlier bricks, and names the brick's
   folder <your output folder>/bricks/<k>_<name>/. Steps that do not depend on each other may be
   started together with `run_subagents`.
3. A brick saves its script, output and log in its folder and reports what it checked.
4. After the last brick, assemble the final file, write manifest.json, and report the path, a short
   summary and any issues.

### Step 3: deliver
Copy each final artifact to the output location, name and format the task requires, then write the
methods summary the task asks for. Never skip a required output: if an artifact failed, deliver your
best attempt and explain why. Long installations should be started once, early, by the sub-agent that
needs them; sub-agents share the same sandbox, so installed packages stay available to later ones.
Waiting: if you ever wait for a job yourself, use one blocking call (`wait <PID>` or `sleep 300`),
never a series of short status checks.
"""


@solver
def condition_note_setup() -> Solver:
    async def solve(state: TaskState, generate: Generate) -> TaskState:
        del generate
        state.user_prompt.text = state.user_prompt.text.rstrip() + "\n\n" + NOTE.strip() + "\n"
        return state

    return solve
