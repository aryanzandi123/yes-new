#!/usr/bin/env python3
"""Config-driven LLM pipeline runner for biology literature research with Gemini 2.5 Pro."""

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

import httpx
from google.genai import types, errors as genai_errors


from dotenv import load_dotenv

from pipeline_config_gemini import PIPELINE_STEPS
from pipeline_types import StepConfig
from visualizer import create_visualization, open_visualization

MAX_ALLOWED_THINKING_BUDGET = 32768
MIN_ALLOWED_THINKING_BUDGET = 128


class PipelineError(RuntimeError):
    """Raised when a pipeline step fails validation or parsing."""


def ensure_env() -> None:
    """Load environment variables and verify the Google API key exists."""
    load_dotenv()
    api_key = os.getenv("GOOGLE_API_KEY")
    if not api_key:
        sys.exit("GOOGLE_API_KEY is not set. Add it to your environment or .env file.")
    


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
        raise PipelineError("PIPELINE_STEPS is empty. Add at least one step in pipeline_config_gemini.py.")

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

    # Gemini sometimes returns leading explanations or multiple JSON blobs. Scan the
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


def call_gemini_model(step: StepConfig, prompt: str) -> str:
    """Execute Gemini API with ALL features: thinking, grounding, URL context, multi-tools."""

    from google import genai as google_genai

    # Get API key
    api_key = os.getenv("GOOGLE_API_KEY")
    if not api_key:
        raise PipelineError("GOOGLE_API_KEY not found in environment")

    client = google_genai.Client(api_key=api_key)

    def build_generation_config(dynamic_enabled: bool) -> tuple[types.GenerateContentConfig, Optional[int], list[types.Tool], Optional[str], int, Optional[str], Optional[str]]:
        config_dict: dict[str, Any] = {}
        applied_thinking_budget: Optional[int] = None

        # 1. Configure thinking (prefer explicit step budgets)
        thinking_notice: Optional[str] = None
        requested_thinking_budget = getattr(step, "thinking_budget", None)
        if requested_thinking_budget is not None:
            clamped_budget = max(MIN_ALLOWED_THINKING_BUDGET, min(requested_thinking_budget, MAX_ALLOWED_THINKING_BUDGET))
            if clamped_budget != requested_thinking_budget:
                thinking_notice = (
                    f"Requested thinking budget {requested_thinking_budget:,} -> using {clamped_budget:,} (API limit)."
                )
            applied_thinking_budget = clamped_budget
            config_dict["thinking_config"] = types.ThinkingConfig(
                thinking_budget=clamped_budget,
                include_thoughts=True,
            )
        elif step.deep_research or step.reasoning_effort == "high":
            applied_thinking_budget = -1
            config_dict["thinking_config"] = types.ThinkingConfig(
                thinking_budget=-1,  # DYNAMIC thinking for 2.5 Pro (model decides)
                include_thoughts=True,
            )

        search_mode_label: Optional[str] = None
        search_notice: Optional[str] = None
        tools: list[types.Tool] = []

        # 2. Configure ALL tools (multi-tool support)
        if step.use_google_search:
            tools.append(types.Tool(google_search=types.GoogleSearch()))
            search_mode_label = "standard"
            if dynamic_enabled and getattr(step, "search_dynamic_mode", False):
                search_notice = (
                    "Gemini 2.5 Pro uses the google_search tool; google_search_retrieval is "
                    "legacy per https://ai.google.dev/gemini-api/docs/google-search."
                )
                call_gemini_model._dynamic_supported = False

        # Add URL context if needed
        if hasattr(step, "use_url_context") and step.use_url_context:
            tools.append(types.Tool(url_context=types.UrlContext()))

        # Add code execution if needed
        if hasattr(step, "use_code_execution") and step.use_code_execution:
            tools.append(types.Tool(code_execution=types.CodeExecution()))

        if tools:
            config_dict["tools"] = tools

        # 3. Add system instructions if provided
        if step.system_prompt:
            config_dict["system_instructions"] = types.Content(
                parts=[types.Part(text=step.system_prompt)]
            )

        # 4. Set other parameters
        output_token_limit = (
            step.max_output_tokens
            if getattr(step, "max_output_tokens", None) is not None
            else 65536
        )
        config_dict["max_output_tokens"] = output_token_limit
        config_dict["temperature"] = 0.3
        config_dict["top_p"] = 0.90

        return (
            types.GenerateContentConfig(**config_dict),
            applied_thinking_budget,
            tools,
            search_mode_label,
            output_token_limit,
            thinking_notice,
            search_notice,
        )

    if not hasattr(call_gemini_model, "_dynamic_supported"):
        call_gemini_model._dynamic_supported = True

    dynamic_search_enabled = bool(getattr(step, "search_dynamic_mode", False)) and call_gemini_model._dynamic_supported

    max_retries = 5
    base_delay = 2.0
    retryable_codes = {408, 409, 429, 500, 502, 503, 504}
    retryable_status = {"RESOURCE_EXHAUSTED", "UNAVAILABLE"}

    def schedule_retry(message: str, delay: float) -> None:
        print(f"   ! {message} Retrying in {delay:.1f}s...", flush=True)
        time.sleep(delay)

    for attempt in range(1, max_retries + 1):
        (
            config,
            applied_thinking_budget,
            tools,
            search_mode_label,
            output_token_limit,
            thinking_notice,
            search_notice,
        ) = build_generation_config(dynamic_search_enabled)
        if not getattr(call_gemini_model, '_dynamic_supported', True):
            dynamic_search_enabled = False
        try:
            if attempt == 1:
                print(f"   Calling gemini-2.5-pro", flush=True)
                if applied_thinking_budget is not None:
                    if applied_thinking_budget < 0:
                        print("    * Thinking: Dynamic (model adjusts internal budget)", flush=True)
                    else:
                        print(f"    * Thinking: {applied_thinking_budget:,} tokens requested", flush=True)
                    if thinking_notice:
                        print(f"      note: {thinking_notice}", flush=True)
                if tools:
                    tool_names: list[str] = []
                    for tool in tools:
                        tool_dict = tool.to_dict() if hasattr(tool, "to_dict") else {}
                        if "google_search" in str(tool_dict):
                            tool_names.append("google_search")
                        elif "url_context" in str(tool_dict):
                            tool_names.append("url_context")
                        elif "code_execution" in str(tool_dict):
                            tool_names.append("code_execution")
                    if tool_names:
                        print(f"    * Tools: {', '.join(tool_names)}", flush=True)
                    if step.use_google_search and search_mode_label:
                        print(f"      - google_search mode: {search_mode_label}", flush=True)
                        if search_notice:
                            print(f"        note: {search_notice}", flush=True)
                print(f"    * Output cap: {output_token_limit:,} tokens", flush=True)
            else:
                print(f"   Retrying call (attempt {attempt}/{max_retries})", flush=True)

            response = client.models.generate_content(
                model="gemini-2.5-pro",
                contents=prompt,
                config=config,
            )

            if response and response.text:
                if hasattr(response, "candidates") and response.candidates:
                    candidate = response.candidates[0]

                    if hasattr(candidate, "grounding_metadata") and candidate.grounding_metadata:
                        grounding_meta = candidate.grounding_metadata

                        if hasattr(grounding_meta, "web_search_queries") and grounding_meta.web_search_queries:
                            print(f"    *  Performed {len(grounding_meta.web_search_queries)} Google searches", flush=True)
                            for i, query in enumerate(grounding_meta.web_search_queries[:3], 1):
                                print(f'      {i}. "{query}"', flush=True)
                        elif step.use_google_search:
                            print("    *   Google Search enabled but NOT USED!", flush=True)

                        if hasattr(grounding_meta, "grounding_chunks") and grounding_meta.grounding_chunks:
                            print(f"    *  Found {len(grounding_meta.grounding_chunks)} web sources", flush=True)

                        if hasattr(grounding_meta, "grounding_supports") and grounding_meta.grounding_supports:
                            print(f"    *  Generated {len(grounding_meta.grounding_supports)} inline citations", flush=True)

                    elif step.use_google_search:
                        print("    *   No grounding metadata - search may have failed!", flush=True)

                if hasattr(response, "usage_metadata"):
                    if hasattr(response.usage_metadata, "thoughts_token_count"):
                        thinking_tokens = response.usage_metadata.thoughts_token_count
                        if thinking_tokens:
                            print(f"    * Thinking: {thinking_tokens:,} tokens used", flush=True)

                return response.text

            raise PipelineError(f"Step '{step.name}' returned empty output.")

        except PipelineError:
            raise
        except genai_errors.APIError as api_error:
            message_parts = [
                f"APIError {api_error.code}",
                api_error.status or "",
                api_error.message or str(api_error),
            ]
            message = " ".join(part for part in message_parts if part).strip()
            lowered_message = message.lower()
            if dynamic_search_enabled and "search grounding is not supported" in lowered_message:
                print("   ! Search grounding not supported for this key; retrying with standard Google Search.", flush=True)
                call_gemini_model._dynamic_supported = False
                dynamic_search_enabled = False
                continue
            should_retry = (
                api_error.code in retryable_codes
                or (api_error.status and api_error.status.upper() in retryable_status)
                or (api_error.message and any(token in api_error.message.lower() for token in ("quota", "rate limit", "overloaded")))
            )
            if should_retry and attempt < max_retries:
                delay = base_delay * (2 ** (attempt - 1))
                schedule_retry(message, delay)
                continue
            raise PipelineError(f"Step '{step.name}' failed: {message}") from api_error
        except (httpx.HTTPError, ConnectionError, TimeoutError, OSError) as transport_error:
            if attempt < max_retries:
                delay = base_delay * (2 ** (attempt - 1))
                schedule_retry(f"{transport_error.__class__.__name__}: {transport_error}", delay)
                continue
            raise PipelineError(f"Step '{step.name}' failed: {transport_error}") from transport_error
        except Exception as error:
            raise PipelineError(f"Step '{step.name}' failed: {error}") from error

    raise PipelineError(f"Step '{step.name}' failed after {max_retries} attempts.")


def run_pipeline(
    user_query: str,
    verbose: bool = False,
    stream: bool = True,
) -> Dict[str, Any]:
    """Execute the configured pipeline and return the final JSON payload."""
    steps = validate_steps(PIPELINE_STEPS)
    current_payload: Optional[Dict[str, Any]] = None

    print(f"\nStarting KAZI Pipeline for: {user_query}")
    print(f"Using: gemini-2.5-pro with MAXIMUM capabilities")
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

            raw_output = call_gemini_model(step, prompt)

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
        description="Run the Gemini 2.5 Pro biology research pipeline with validation and visualization.")
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