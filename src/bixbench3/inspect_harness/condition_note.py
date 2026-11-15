"""v4 dag-orchestrator: append the orchestration note to the end of the task prompt.

The original task prompt (paper context, data, required outputs) is left untouched; this
note only adds a way of organizing the work with the `run_subagent` tool. It contains no
ground truth, reference outputs or grader-derived values, and it states that the task's own
output requirements take precedence.
"""

from __future__ import annotations

from inspect_ai.solver import Generate, Solver, TaskState, solver

CONDITION = "v4-dag-orchestrator"

NOTE = """## Experiment Note: Work as an Orchestrator of Sub-Agents

Everything above (data, methods, required outputs, file names and formats, time limit) still applies
and takes precedence over this note. This note only changes how you organize the work.

You are the ORCHESTRATOR. Do not compute artifacts yourself: plan, delegate with `run_subagent`,
verify and store. A sub-agent starts with an empty context; its brief is all it knows, so every brief
must be self-contained.

### Step 1: dependency plan
Call a sub-agent with role "planner". Give it the full task text above and the list of files under
/data. It must return a dependency graph of every required output artifact: id, exact specification
(quoted from the task), required inputs (raw data and/or other artifacts), expected output file, and
depth (depth 1 = needs only raw data; otherwise 1 + the largest depth among its input artifacts).
Check that the graph has no cycle and covers every output the task requires, then save it as
/workspace/work/pipeline/plan.json.

### Step 2: build layer by layer
For depth = 1, 2, ...: for each artifact of that depth, call one sub-agent with role
"artifact:<id>" and a brief that states:
- GOAL: the artifact's exact specification, quoted from the task;
- ALLOWED INPUTS: the exact paths it may read (the raw data it needs and the stored outputs of the
  artifacts it depends on), and nothing else;
- FORBIDDEN: every other file or artifact, and any expected value, reference result or published
  number used as a target;
- OUTPUT: /workspace/work/pipeline/artifacts/<depth>_<id>/, holding the final file and a
  manifest.json (inputs actually read, scripts, status, known issues);
- the ARTIFACT BUILDER RULES below, copied into the brief.
When it returns, verify yourself that the file exists, is non-empty, has the required format, and that
manifest.json lists only allowed inputs. If it failed, call a new sub-agent once with the same brief
plus the error. Update plan.json after every artifact.

### Artifact builder rules (copy them into every artifact brief)
1. First break the artifact into ordered elementary steps (one operation each, with its inputs,
   output and a success check) and save them as bricks_plan.json in your output folder.
2. Then call one sub-agent per step, in order, with role "brick:<k>-<name>". Each brick brief repeats
   the ALLOWED and FORBIDDEN inputs, adds the outputs of the earlier bricks, and names the brick's
   folder <your output folder>/bricks/<k>_<name>/.
3. A brick saves its script, output and log in its folder and reports what it checked.
4. After the last brick, assemble the final file, write manifest.json, and report the path, a short
   summary and any issues.

### Step 3: deliver
Copy each final artifact to the output location, name and format the task requires, then write the
methods summary the task asks for. Never skip a required output: if an artifact failed, deliver your
best attempt and explain why. Long installations should be started once, early, by the sub-agent that
needs them; sub-agents share the same sandbox, so installed packages stay available to later ones.
"""


@solver
def condition_note_setup() -> Solver:
    async def solve(state: TaskState, generate: Generate) -> TaskState:
        del generate
        state.user_prompt.text = state.user_prompt.text.rstrip() + "\n\n" + NOTE.strip() + "\n"
        return state

    return solve
