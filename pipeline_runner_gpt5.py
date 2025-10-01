#!/usr/bin/env python3
"""Config-driven LLM pipeline runner for biology literature research with OpenAI GPT-5."""

from __future__ import annotations

import argparse
import json
from copy import deepcopy
import os
import sys
import time
from collections.abc import Iterable
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from openai import APIError, OpenAI

from dotenv import load_dotenv

from pipeline_config_gpt5 import PIPELINE_STEPS
from pipeline_types import StepConfig
from visualizer import create_visualization, open_visualization

MAX_ALLOWED_THINKING_BUDGET = 32768
MIN_ALLOWED_THINKING_BUDGET = 128

_openai_client: Optional[OpenAI] = None


def get_openai_client() -> OpenAI:
    """Create (or reuse) a singleton OpenAI client."""

    global _openai_client
    if _openai_client is None:
        _openai_client = OpenAI()
    return _openai_client


class PipelineError(RuntimeError):
    """Raised when a pipeline step fails validation or parsing."""


def ensure_env() -> None:
    """Load environment variables and verify the OpenAI API key exists."""
    load_dotenv()
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        sys.exit("OPENAI_API_KEY is not set. Add it to your environment or .env file.")
    


def validate_steps(steps: Iterable[StepConfig]) -> List[StepConfig]:
    """Ensure step configuration is sane before executing the pipeline."""
    seen_names: set[str] = set()
    validated: List[StepConfig] = []

    for step in steps:
        if step.name in seen_names:
            raise PipelineError(f"Duplicate step name detected: {step.name}")
        if not step.expected_columns:
            raise PipelineError(f"Step '{step.name}' must declare expected_columns.")
        seen_names.add(step.name)
        validated.append(step)

    if not validated:
        raise PipelineError("PIPELINE_STEPS is empty. Add at least one step in pipeline_config_gpt5.py.")

    return validated


def strip_code_fences(text: str) -> str:
    """Remove surrounding Markdown code fences if present."""
    stripped = text.strip()
    if stripped.startswith("```") and stripped.endswith("```"):
        stripped = stripped.strip("`").strip()
        if stripped.lower().startswith("json"):
            stripped = stripped[4:].lstrip()
        elif stripped.lower().startswith("csv"):
            stripped = stripped[3:].lstrip()
    return stripped






def parse_json_output(
    text: str,
    expected_fields: List[str],
    previous_payload: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Parse model output into a JSON object, merging with prior payload when needed."""
    cleaned = strip_code_fences(text)
    decoder = json.JSONDecoder()
    idx = 0
    data_segments: List[Dict[str, Any]] = []
    last_end: Optional[int] = None
    required = [field for field in expected_fields if field != "ndjson"]

    # GPT-5 sometimes returns leading explanations or multiple JSON blobs. Scan the
    # string and decode each JSON payload until we gather the fields we need.
    while idx < len(cleaned):
        if cleaned[idx].isspace():
            idx += 1
            continue
        try:
            obj, end = decoder.raw_decode(cleaned, idx)
        except json.JSONDecodeError:
            idx += 1
            continue
        if end <= idx:
            idx += 1
            continue

        last_end = end
        idx = end

        if isinstance(obj, dict):
            data_segments.append(obj)
        elif isinstance(obj, list):
            for item in obj:
                if isinstance(item, dict):
                    data_segments.append(item)

        have_required = False
        if data_segments:
            merged_preview: Dict[str, Any] = {}
            for segment in data_segments:
                merged_preview.update(segment)
            have_required = all(field in merged_preview for field in required)
            if have_required and ("ndjson" not in expected_fields or "ndjson" in merged_preview):
                break

    combined: Dict[str, Any] = deepcopy(previous_payload) if previous_payload else {}
    for segment in data_segments:
        combined.update(segment)

    if not combined and not data_segments:
        raise PipelineError("Model must return a JSON object.")

    missing_required = [field for field in required if field not in combined]
    if missing_required:
        raise PipelineError(f"JSON missing required keys: {missing_required}")

    remainder = cleaned[last_end:].strip() if last_end is not None else ""

    if "ndjson" in expected_fields and "ndjson" not in combined and remainder:
        ndjson_lines: List[str] = []
        cursor = 0
        while cursor < len(remainder):
            while cursor < len(remainder) and remainder[cursor].isspace():
                cursor += 1
            if cursor >= len(remainder) or remainder[cursor] not in "{[":
                break
            try:
                _, seg_end = decoder.raw_decode(remainder, cursor)
            except json.JSONDecodeError:
                break
            segment = remainder[cursor:seg_end].strip()
            if not segment:
                break
            ndjson_lines.append(segment)
            if seg_end <= cursor:
                break
            cursor = seg_end
        if ndjson_lines:
            combined["ndjson"] = ndjson_lines

    missing = [field for field in expected_fields if field not in combined]
    if missing:
        raise PipelineError(f"JSON missing required keys: {missing}")

    return combined


def generate_snapshot_payload(
    previous_payload: Optional[Dict[str, Any]],
    step_name: str,
    expected_fields: Sequence[str],
) -> Dict[str, Any]:
    """Build snapshot and ndjson output locally for step3_snapshot."""
    if previous_payload is None:
        raise PipelineError(f"Step '{step_name}' requires ctx_json from a prior step.")

    ctx_json = previous_payload.get("ctx_json")
    if not isinstance(ctx_json, dict):
        raise PipelineError(f"Step '{step_name}' requires ctx_json as a JSON object.")

    main_symbol = ctx_json.get("main")
    if not isinstance(main_symbol, str) or not main_symbol.strip():
        raise PipelineError(f"ctx_json missing valid 'main' for step '{step_name}'.")
    main_symbol = main_symbol.strip()

    def _normalize_str_list(value: Any) -> List[str]:
        if isinstance(value, list):
            result: List[str] = []
            for item in value:
                if isinstance(item, str):
                    item = item.strip()
                    if item:
                        result.append(item)
                elif item is not None:
                    result.append(str(item))
            return result
        if isinstance(value, str):
            value = value.strip()
            return [value] if value else []
        return []

    def _normalize_confidence(value: Any) -> Optional[float]:
        if isinstance(value, (int, float)):
            return float(value)
        if isinstance(value, str):
            value = value.strip()
            if not value:
                return None
            try:
                return float(value)
            except ValueError:
                return None
        return None

    interactors = ctx_json.get("interactors")
    if not isinstance(interactors, list):
        interactors = []

    snapshot_interactors: List[Dict[str, Any]] = []
    ndjson_lines: List[str] = []

    for raw_interactor in interactors:
        if not isinstance(raw_interactor, dict):
            continue

        primary = raw_interactor.get("primary")
        arrow = raw_interactor.get("arrow")
        direction = raw_interactor.get("direction")
        intent = raw_interactor.get("intent")
        pmids = _normalize_str_list(raw_interactor.get("pmids"))
        confidence = _normalize_confidence(raw_interactor.get("confidence"))
        evidence = raw_interactor.get("evidence")
        support_summary = raw_interactor.get("support_summary")
        multiple_mechanisms = raw_interactor.get("multiple_mechanisms")

        functions_value = raw_interactor.get("functions")
        minimal_functions: List[Dict[str, Any]] = []
        if isinstance(functions_value, list):
            for raw_function in functions_value:
                if not isinstance(raw_function, dict):
                    continue
                function_name = raw_function.get("function")
                if not function_name:
                    continue
                function_entry: Dict[str, Any] = {"function": function_name}
                function_arrow = raw_function.get("arrow")
                if function_arrow:
                    function_entry["arrow"] = function_arrow
                function_pmids = _normalize_str_list(raw_function.get("pmids"))
                if function_pmids:
                    function_entry["pmids"] = function_pmids
                function_confidence = _normalize_confidence(raw_function.get("confidence"))
                if function_confidence is not None:
                    function_entry["confidence"] = function_confidence
                # Include new fields
                for field in ["note", "normal_role", "cellular_process", "biological_consequence", "specific_effects", "mechanism_subset"]:
                    if field in raw_function:
                        function_entry[field] = raw_function[field]
                minimal_functions.append(function_entry)

        interactor_entry: Dict[str, Any] = {}
        if primary:
            interactor_entry["primary"] = primary
        if isinstance(direction, str) and direction.strip():
            interactor_entry["direction"] = direction.strip()
        if arrow:
            interactor_entry["arrow"] = arrow
        if intent:
            interactor_entry["intent"] = intent
        if pmids:
            interactor_entry["pmids"] = pmids
        if confidence is not None:
            interactor_entry["confidence"] = confidence
        if evidence:
            interactor_entry["evidence"] = evidence
        if support_summary:
            interactor_entry["support_summary"] = support_summary
        if multiple_mechanisms:
            interactor_entry["multiple_mechanisms"] = multiple_mechanisms
        interactor_entry["functions"] = minimal_functions

        snapshot_interactors.append(interactor_entry)

        ndjson_obj: Dict[str, Any] = {"main": main_symbol}
        ndjson_obj.update(interactor_entry)
        ndjson_lines.append(json.dumps(ndjson_obj, ensure_ascii=False, separators=(",", ":")))

    snapshot_json = {"main": main_symbol, "interactors": snapshot_interactors}
    result: Dict[str, Any] = {"ctx_json": ctx_json, "snapshot_json": snapshot_json, "ndjson": ndjson_lines}
    result["step_json"] = {"step": step_name, "rows": len(ndjson_lines)}

    for field in expected_fields:
        if field not in result:
            result[field] = None

    return result


def dumps_compact(data: Any) -> str:
    """Serialize data to compact JSON for inclusion in prompts."""
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"))


def build_prompt(
    step: StepConfig,
    prior_payload: Optional[Dict[str, Any]],
    user_query: str,
    is_first_step: bool,
) -> str:
    """Prepare the prompt that will be sent to the model for this step."""
    expected_fields = [field.strip() for field in step.expected_columns]
    instructions = [
        "Return ONLY valid JSON. No markdown fences, no extra text, no commentary.",
        f"Keys required: {', '.join(expected_fields)}.",
        "Preserve existing ctx_json content; extend it with this step's additions.",
    ]

    if is_first_step and user_query:
        instructions.append(f"Query protein: {user_query}")

    user_template = step.prompt_template.replace("{user_query}", user_query)

    # Handle iterative steps - replace placeholders with actual history
    if prior_payload and "ctx_json" in prior_payload:
        ctx_json = prior_payload["ctx_json"]
        
        # Replace interactor_history placeholder
        if "{ctx_json.interactor_history}" in user_template:
            interactor_history = ctx_json.get("interactor_history", [])
            user_template = user_template.replace(
                "{ctx_json.interactor_history}", 
                str(interactor_history)
            )
        
        # Replace search_history placeholder
        if "{ctx_json.search_history}" in user_template:
            search_history = ctx_json.get("search_history", [])
            user_template = user_template.replace(
                "{ctx_json.search_history}",
                str(search_history)
            )
        
        # Replace function_history placeholder
        if "{ctx_json.function_history}" in user_template:
            function_history = ctx_json.get("function_history", {})
            user_template = user_template.replace(
                "{ctx_json.function_history}",
                str(function_history)
            )

    prompt_sections = ["\n".join(instructions), user_template.strip()]

    if prior_payload:
        prompt_sections.append("Prior JSON:\n" + dumps_compact(prior_payload))

    return "\n\n".join(section for section in prompt_sections if section)


def call_openai_model(
    step: StepConfig,
    prompt: str,
    *,
    deep_research_mode: Optional[str] = None,
) -> str:
    """Execute the OpenAI Responses API with maximum reasoning and search capabilities."""

    client = get_openai_client()

    def _build_request_kwargs(include_deep_research: bool) -> Dict[str, Any]:
        reasoning: Dict[str, Any] = {}

        requested_budget = getattr(step, "thinking_budget", None)
        if requested_budget is None:
            requested_budget = MAX_ALLOWED_THINKING_BUDGET
        clamped_budget = max(MIN_ALLOWED_THINKING_BUDGET, min(requested_budget, MAX_ALLOWED_THINKING_BUDGET))
        reasoning["effort"] = (step.reasoning_effort or "high")
        reasoning["budget_tokens"] = clamped_budget

        tools: List[Dict[str, Any]] = []
        if getattr(step, "use_web_search", False):
            web_search_config: Dict[str, Any] = {"type": "web_search", "web_search": {}}
            if getattr(step, "web_search_dynamic_mode", False):
                web_search_config["web_search"]["mode"] = "aggressive"
                if step.web_search_dynamic_threshold is not None:
                    web_search_config["web_search"]["threshold"] = step.web_search_dynamic_threshold
            else:
                web_search_config["web_search"]["mode"] = "focused"
            tools.append(web_search_config)

        messages: List[Dict[str, Any]] = []
        if step.system_prompt:
            messages.append(
                {
                    "role": "system",
                    "content": [{"type": "text", "text": step.system_prompt}],
                }
            )
        messages.append({"role": "user", "content": [{"type": "text", "text": prompt}]})

        kwargs: Dict[str, Any] = {
            "model": step.model,
            "input": messages,
            "reasoning": reasoning,
            "temperature": 0.2,
            "top_p": 0.9,
            "max_output_tokens": step.max_output_tokens or 65536,
            "response_format": {"type": "json_object"},
            "parallel_tool_calls": True,
        }

        if tools:
            kwargs["tools"] = tools

        extra_body: Dict[str, Any] = {}
        if include_deep_research and tools:
            mode = deep_research_mode or ("comprehensive" if getattr(step, "web_search_dynamic_mode", False) else "focused")
            extra_body["deep_research"] = {"enable": True, "mode": mode}

        if extra_body:
            kwargs["extra_body"] = extra_body

        return kwargs

    def _extract_output_text(response: Any) -> str:
        if hasattr(response, "output_text") and response.output_text:
            return response.output_text

        texts: List[str] = []
        for item in getattr(response, "output", []) or []:
            for block in getattr(item, "content", []) or []:
                text = getattr(block, "text", None)
                if text:
                    texts.append(text)
        if texts:
            return "".join(texts)

        raise PipelineError("OpenAI response did not contain text output")

    max_retries = 5
    base_delay = 2.0
    include_deep_research = bool(deep_research_mode) or bool(getattr(step, "deep_research", False))

    for attempt in range(1, max_retries + 1):
        try:
            kwargs = _build_request_kwargs(include_deep_research)

            if attempt == 1:
                print(f"   Calling {step.model}", flush=True)
                budget = kwargs["reasoning"].get("budget_tokens")
                if budget:
                    print(f"    * Thinking budget: {budget:,} tokens", flush=True)
                if kwargs.get("tools"):
                    tool_labels = []
                    for tool in kwargs["tools"]:
                        mode = tool.get("web_search", {}).get("mode")
                        label = "web_search" + (f"[{mode}]" if mode else "")
                        tool_labels.append(label)
                    print(f"    * Tools: {', '.join(tool_labels)}", flush=True)
                if include_deep_research and "extra_body" in kwargs:
                    mode = kwargs["extra_body"]["deep_research"].get("mode")
                    print(f"    * Deep Research mode: {mode}", flush=True)
                print(f"    * Output cap: {kwargs['max_output_tokens']:,} tokens", flush=True)
            else:
                print(f"   Retrying call (attempt {attempt}/{max_retries})", flush=True)

            response = client.responses.create(**kwargs)
            output_text = _extract_output_text(response)

            usage = getattr(response, "usage", None)
            if usage:
                reasoning_tokens = getattr(usage, "reasoning_tokens", None)
                if reasoning_tokens:
                    print(f"    * Thinking used: {reasoning_tokens:,} tokens", flush=True)
                web_results = getattr(usage, "web_search_queries", None)
                if web_results:
                    print(f"    * OpenAI web search queries: {len(web_results)}", flush=True)

            return output_text

        except APIError as api_error:
            message = str(api_error)
            status = getattr(api_error, "status_code", None)
            should_retry = status in {408, 409, 429, 500, 502, 503, 504}
            if include_deep_research and "deep research" in message.lower():
                print("   ! Deep Research not available for this key; retrying without it.", flush=True)
                include_deep_research = False
                continue
            if should_retry and attempt < max_retries:
                delay = base_delay * (2 ** (attempt - 1))
                print(f"   ! {message} Retrying in {delay:.1f}s...", flush=True)
                time.sleep(delay)
                continue
            raise PipelineError(f"Step '{step.name}' failed: {message}") from api_error
        except Exception as error:
            raise PipelineError(f"Step '{step.name}' failed: {error}") from error

    raise PipelineError(f"Step '{step.name}' failed after {max_retries} attempts.")

def run_pipeline(
    user_query: str,
    verbose: bool = False,
    stream: bool = True,
    deep_research_mode: Optional[str] = None,
) -> Dict[str, Any]:
    """Execute the configured pipeline and return the final JSON payload."""
    steps = validate_steps(PIPELINE_STEPS)
    current_payload: Optional[Dict[str, Any]] = None

    print(f"\nStarting KAZI Pipeline for: {user_query}")
    print(f"Using: {PIPELINE_STEPS[0].model} with MAXIMUM capabilities")
    print("="*60)

    for index, step in enumerate(steps):
        if stream or verbose:
            print(f"\n[Step {index + 1}/{len(steps)}] {step.name}", flush=True)

        if step.name == "step3_snapshot":
            current_payload = generate_snapshot_payload(
                current_payload,
                step.name,
                list(step.expected_columns),
            )
            if verbose:
                print("  → Snapshot generated locally; skipping model call.\n")
        else:
            # Check if this is an iterative step that needs accumulated data
            is_iterative_step = any(suffix in step.name for suffix in ['1b', '1c', '2b', '2c'])
            
            if is_iterative_step and current_payload:
                # Update interactor_history for step 1b/1c
                if '1b' in step.name or '1c' in step.name:
                    ctx_json = current_payload.get("ctx_json", {})
                    interactors = ctx_json.get("interactors", [])
                    interactor_symbols = [i.get("primary") for i in interactors if i.get("primary")]
                    ctx_json["interactor_history"] = interactor_symbols
                    current_payload["ctx_json"] = ctx_json
                    if verbose:
                        print(f"  → Updated interactor_history: {len(interactor_symbols)} interactors")
                
                # Update function_history for step 2b/2c  
                elif '2b' in step.name or '2c' in step.name:
                    ctx_json = current_payload.get("ctx_json", {})
                    all_functions = {}
                    for interactor in ctx_json.get("interactors", []):
                        func_list = []
                        for func in interactor.get("functions", []):
                            func_list.append(func.get("function"))
                        if func_list:
                            all_functions[interactor.get("primary")] = func_list
                    ctx_json["function_history"] = all_functions
                    current_payload["ctx_json"] = ctx_json
                    if verbose:
                        total_funcs = sum(len(f) for f in all_functions.values())
                        print(f"  → Updated function_history: {total_funcs} functions across {len(all_functions)} interactors")
            
            prompt = build_prompt(step, current_payload, user_query, index == 0)

            if verbose:
                print("Prompt:\n" + prompt + "\n")

            raw_output = call_openai_model(step, prompt, deep_research_mode=deep_research_mode)

            if verbose:
                print("Model output:\n" + raw_output + "\n")

            current_payload = parse_json_output(
                raw_output,
                list(step.expected_columns),
                previous_payload=current_payload,
            )
            
            # Show progress
            if current_payload and "ctx_json" in current_payload:
                interactor_count = len(current_payload["ctx_json"].get("interactors", []))
                total_functions = 0
                for interactor in current_payload["ctx_json"].get("interactors", []):
                    total_functions += len(interactor.get("functions", []))
                
                if '1' in step.name:  # Steps 1a, 1b, 1c
                    print(f"  → Found {interactor_count} interactors total", flush=True)
                elif '2' in step.name:  # Steps 2a, 2b, 2c
                    print(f"  → Mapped {total_functions} functions for {interactor_count} interactors", flush=True)
            
        if stream and not verbose:
            # Don't print full JSON in stream mode unless verbose
            pass

    if current_payload is None:
        raise PipelineError("Pipeline completed without returning JSON data.")
    

    return current_payload


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run the OpenAI GPT-5 biology research pipeline with validation and visualization.")
    parser.add_argument(
        "query",
        nargs="?",
        help="User research question passed to the first pipeline step.",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Print prompts and raw model outputs for debugging.",
    )
    parser.add_argument(
        "--no-stream",
        action="store_true",
        help="Disable streaming previews after each step.",
    )
    parser.add_argument(
        "--output",
        type=str,
        help="Where to write the final JSON payload (default: <query>_pipeline.json).",
    )
    parser.add_argument(
        "--no-viz",
        action="store_true",
        help="Disable automatic visualization generation.",
    )
    parser.add_argument(
        "--viz-only",
        type=str,
        help="Skip pipeline and create visualization from existing JSON file.",
    )
    parser.add_argument(
        "--deep-research",
        choices=["focused", "comprehensive"],
        help="Enable OpenAI Deep Research augmentation when supported by the API.",
    )


    args = parser.parse_args()

    # Visualization-only mode
    if args.viz_only:
        json_file = Path(args.viz_only)
        if not json_file.exists():
            parser.error(f"JSON file not found: {json_file}")
        
        print(f"Creating visualization from: {json_file}")
        html_file = create_visualization(json_file)
        open_visualization(html_file)
        print("Visualization opened in browser!")
        return

    # Normal pipeline mode
    if not args.query:
        try:
            args.query = input("Enter query protein: ").strip()
        except EOFError:
            args.query = ""

    if not args.query:
        parser.error("query is required")

    ensure_env()

    final_payload = run_pipeline(
        user_query=args.query,
        verbose=args.verbose,
        stream=not args.no_stream,
        deep_research_mode=args.deep_research,
    )

    output_path = Path(args.output) if args.output else Path(f"{args.query}_pipeline.json")
    output_path.write_text(
        json.dumps(final_payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    ndjson_path: Optional[Path] = None
    ndjson_content = final_payload.get("ndjson")
    if ndjson_content:
        if isinstance(ndjson_content, list):
            ndjson_text = "\n".join(
                item if isinstance(item, str) else json.dumps(item, ensure_ascii=False)
                for item in ndjson_content
            )
        else:
            ndjson_text = str(ndjson_content)

        ndjson_path = output_path.with_suffix(".ndjson")
        ndjson_path.write_text(ndjson_text.rstrip() + "\n", encoding="utf-8")

    # Print summary
    print("\n" + "="*80)
    print("PIPELINE COMPLETE")
    print("="*80)
    
    if "ctx_json" in final_payload:
        interactors = final_payload["ctx_json"].get("interactors", [])
        total_functions = sum(len(i.get("functions", [])) for i in interactors)
        print(f"✓ Found {len(interactors)} interactors")
        print(f"✓ Mapped {total_functions} biological functions")

    
    print(f"\nSaved final JSON to {output_path}")
    if ndjson_path:
        print(f"Saved NDJSON to {ndjson_path}")

    # Generate visualization unless disabled
    if not args.no_viz:
        print("\nGenerating interactive visualization...")
        html_path = output_path.with_suffix(".html")
        viz_file = create_visualization(output_path, html_path)
        open_visualization(viz_file)
        print(f"Visualization saved to: {viz_file}")
        print("Opening visualization in browser...")


if __name__ == "__main__":
    main()