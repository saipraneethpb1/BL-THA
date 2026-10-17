"""Shared helper: run a model step, check its output in code, and retry once with the
problems spelled out. Whatever is still wrong after the retry is returned as issues for
the human reviewer, never silently patched."""

from collections.abc import Callable

from pydantic import BaseModel

from scope.llm import Effort, StructuredLLM


async def run_checked[T: BaseModel](
    llm: StructuredLLM,
    *,
    step: str,
    model: str,
    system: str,
    user: str,
    schema: type[T],
    effort: Effort,
    check: Callable[[T], list[str]],
    retries: int = 1,
) -> tuple[T, list[str]]:
    out = await llm.run(
        step=step, model=model, system=system, user=user, schema=schema, effort=effort
    )
    problems = check(out)
    attempt = 0
    while problems and attempt < retries:
        attempt += 1
        feedback = "\n".join(f"- {p}" for p in problems)
        out = await llm.run(
            step=f"{step}.retry",
            model=model,
            system=system,
            schema=schema,
            effort=effort,
            user=f"{user}\n\n<problems_with_your_previous_answer>\n{feedback}\n"
            "Fix every problem above and answer again in full.\n</problems_with_your_previous_answer>",
        )
        problems = check(out)
    return out, problems
