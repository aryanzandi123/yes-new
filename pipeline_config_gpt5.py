"""Best‑prompts pipeline configuration for OpenAI GPT-5 (runner‑compatible)
Non‑hallucinative, citation‑first, multi‑pass discovery; tuned to your current runner.

Key alignment with runner
-------------------------
• Prompts embed all guardrails in the user content (runner uses `system_instructions`, which may be ignored by SDK).
• Fields match runner expectations: ctx_json/interactors/functions with evidence, plus snapshot support fields.
• Uses OpenAI web search on every LLM step; snapshot handled locally by the runner.
"""
from __future__ import annotations

from pipeline_types import StepConfig

MAX_THINKING_TOKENS = 65536
MAX_OUTPUT_TOKENS = 65536
DYNAMIC_SEARCH_THRESHOLD = 0.0

# ------------------------
# SHARED TEXT BLOCKS
# ------------------------

STRICT_GUARDRAILS = """STRICT NON‑HALLUCINATION RULES (OVERRIDES ANY EARLIER TEXT):
- Do NOT guess or infer. Add claims ONLY when supported by primary, citable evidence.
- Every new claim MUST include a verifiable PMID and/or DOI (prefer PMIDs).
- If no credible source is found with OpenAI web search, OMIT the claim entirely.
- Never invent paper titles, PMIDs, DOIs, authors, journals, or years.
- Prefer primary studies; if a review is used, label it 'review' and set confidence ≤ 0.5.
- Output ONLY valid JSON (no prose, no markdown)."""

SCHEMA_HELP = """SCHEMA
ctx_json = {
  'main': <HGNC>,
  'interactors': [{
      'primary': <HGNC>,
      'direction': 'main_to_primary'|'primary_to_main'|'bidirectional',
      'arrow': 'activates'|'inhibits'|'binds',
      'intent': 'phosphorylation'|'dephosphorylation'|'ubiquitination'|'deubiquitination'|'sumoylation'|'neddylation'|'acetylation'|'deacetylation'|'methylation'|'demethylation'|'glycosylation'|'cleavage'|'recruitment'|'localization'|'sequestration'|'transport'|'scaffolding'|'binding'|'competition'|'unknown',
      'multiple_mechanisms': true|false,
      'mechanism_details': [<strings>],
      'pmids': ['########'],
      'evidence': [{
          'pmid':'########','doi':'<doi?>','paper_title':'...','authors':'...','journal':'...',
          'year':YYYY,'assay':'...','species':'human|mouse|cell',
          'relevant_quote':'<=200 chars excerpt supporting the claim'
      }],
      'confidence': 0.00-1.00,
      'support_summary': '<=160 chars summary',
      'functions': [{
          'function':'<short>', 'arrow':'activates'|'inhibits',
          'cellular_process':'<mechanism>', 'biological_consequence':'<cascade>',
          'specific_effects':[...], 'pmids':['########',...], 'confidence':0.00-1.00,
          'mechanism_id':'<link to mechanism_details index or id>',
          'note':'<optional>', 'normal_role':'<optional>', 'mechanism_subset':'<optional>'
      }]
  }],
  'interactor_history':[<HGNC>,...],
  'function_history':{'PROTEIN':[functions...]},
  'search_history':["search terms used ..."]
}
"""

CONFIDENCE_GUIDE = """CONFIDENCE GUIDE
- 0.90–0.99: ≥2 independent primary papers in human / strong convergent assays.
- 0.75–0.89: ≥1 primary paper in human or multiple in vitro/animal with clear mechanism.
- 0.50–0.74: primary evidence exists but indirect/limited OR high‑quality review only (label 'review').
- <0.50: do not include."""

FIND_SYNONYMS = """Search strategy
- Query also with aliases/synonyms for the MAIN protein and interactors (HGNC, UniProt, legacy symbols).
- Log all crafted queries to ctx_json.search_history."""

# ------------------------
# PIPELINE STEPS
# ------------------------

PIPELINE_STEPS = [
    # 1a — Initial interactor discovery (strict, evidence‑first)
    StepConfig(
        name="step1a_interactors",
        model="gpt-5.0",
        deep_research=False,
        reasoning_effort="high",
        use_web_search=True,
        thinking_budget=MAX_THINKING_TOKENS,
        max_output_tokens=MAX_OUTPUT_TOKENS,
        web_search_dynamic_mode=True,
        web_search_dynamic_threshold=DYNAMIC_SEARCH_THRESHOLD,
        expected_columns=["ctx_json", "step_json"],
        system_prompt=None,
        prompt_template=(
            STRICT_GUARDRAILS + "\n\n"
            "KAZI 1a — FIND DIRECT INTERACTORS (HGNC symbols only).\n"
            "INPUT: user_query = {user_query}\n\n"
            "TASK\n"
            "- Use OpenAI web search for experimentally supported, DIRECT interactors of the MAIN protein.\n"
            "- Prioritize physical interactions (binding/complex/recruitment/PTMs) or immediate regulation.\n"
            "- EXCLUDE predicted/AI‑only resources unless backed by primary experiments.\n"
            "- For each interactor: set direction ('main_to_primary' if MAIN acts on interactor; 'primary_to_main' if interactor acts on MAIN). If both directions are evidenced, use 'bidirectional'.\n"
            "- Choose arrow: 'activates'|'inhibits' if explicitly stated; otherwise 'binds'.\n\n"
            + FIND_SYNONYMS + "\n\n"
            "MULTI‑MECHANISM PAIRS\n"
            "- If the pair shows multiple/contradictory mechanisms, set arrow='binds', multiple_mechanisms=true, add 'mechanism_details', and emit distinct function items later.\n\n"
            + SCHEMA_HELP + "\n\n"
            + CONFIDENCE_GUIDE + "\n\n"
            "QUALITY BEFORE OUTPUT\n"
            "- Human data preferred; otherwise label species.\n"
            "- Extract brief exact quotes supporting the claim. No paraphrased quotes.\n"
            "- De‑duplicate interactors; keep strongest direction/arrow and merge evidence.\n\n"
            "Return ONLY a JSON object with ctx_json and step_json={'step':'step1a_interactors','count':len(ctx_json['interactors'])}."
        ),
    ),

    # 1b — Missed interactors pass
    StepConfig(
        name="step1b_interactors",
        model="gpt-5.0",
        deep_research=False,
        reasoning_effort="high",
        use_web_search=True,
        thinking_budget=MAX_THINKING_TOKENS,
        max_output_tokens=MAX_OUTPUT_TOKENS,
        web_search_dynamic_mode=True,
        web_search_dynamic_threshold=DYNAMIC_SEARCH_THRESHOLD,
        expected_columns=["ctx_json", "step_json"],
        system_prompt=None,
        prompt_template=(
            STRICT_GUARDRAILS + "\n\n"
            "KAZI 1b — FIND ADDITIONAL DIRECT INTERACTORS NOT ALREADY LISTED.\n"
            "YOU ALREADY FOUND: {ctx_json.interactor_history}\n\n"
            + FIND_SYNONYMS + "\n\n"
            "RULES\n"
            "- Same evidence rules as 1a.\n"
            "- No duplicates; merge evidence if an interactor reappears.\n"
            "- If nothing new is found: preserve ctx_json; set step_json={'step':'step1b_interactors','count':0,'message':'No additional interactors found'}.\n\n"
            + SCHEMA_HELP + "\n\n"
            "Return ONLY JSON with ctx_json and step_json."
        ),
    ),

    # 1c — Final coverage sweep
    StepConfig(
        name="step1c_interactors",
        model="gpt-5.0",
        deep_research=False,
        reasoning_effort="high",
        use_web_search=True,
        thinking_budget=MAX_THINKING_TOKENS,
        max_output_tokens=MAX_OUTPUT_TOKENS,
        web_search_dynamic_mode=True,
        web_search_dynamic_threshold=DYNAMIC_SEARCH_THRESHOLD,
        expected_columns=["ctx_json", "step_json"],
        system_prompt=None,
        prompt_template=(
            STRICT_GUARDRAILS + "\n\n"
            "KAZI 1c — COMPREHENSIVE SWEEP FOR ANY MISSED DIRECT INTERACTORS.\n"
            "YOU ALREADY FOUND: {ctx_json.interactor_history}\n\n"
            "- Try alternative phrasings (binds/associates/complex with/recruits/antagonizes/activates/inhibits).\n"
            "- Consider adaptor proteins if direct binding/regulation is shown (include).\n"
            "- Preserve/merge; no duplicates.\n\n"
            + SCHEMA_HELP + "\n\n"
            "If none: step_json={'step':'step1c_interactors','count':0,'message':'Comprehensive search complete'}.\n"
            "Return ONLY JSON."
        ),
    ),

    # 2a — Functions & processes per interactor
    StepConfig(
        name="step2a_functions",
        model="gpt-5.0",
        deep_research=False,
        reasoning_effort="high",
        use_web_search=True,
        thinking_budget=MAX_THINKING_TOKENS,
        max_output_tokens=MAX_OUTPUT_TOKENS,
        web_search_dynamic_mode=True,
        web_search_dynamic_threshold=DYNAMIC_SEARCH_THRESHOLD,
        expected_columns=["ctx_json", "step_json"],
        system_prompt=None,
        prompt_template=(
            STRICT_GUARDRAILS + "\n\n"
            "KAZI 2a — MAP BIOLOGICAL FUNCTIONS PER INTERACTION (mechanism vs cascade).\n"
            "INPUT: ctx_json from 1c.\n\n"
            "FOR EACH interactor entry, USE OPENAI WEB SEARCH to find functions DIRECTLY CAUSED by that interaction (respect direction).\n"
            "- If direction='main_to_primary': functions belong to the PRIMARY protein.\n"
            "- If direction='primary_to_main': functions belong to the MAIN protein.\n"
            "- For bidirectional pairs, produce separate function items per direction/effect.\n\n"
            "OUTPUT RULES\n"
            "- Every function MUST have ≥1 PMID/DOI (primary preferred).\n"
            "- 'cellular_process' = immediate molecular mechanism; 'biological_consequence' = downstream cascade (must not duplicate).\n"
            "- For multi_mechanism pairs: create distinct function entries tied via 'mechanism_id'.\n"
            "- Do not group distinct functions; emit separate items (maximize evidenced coverage).\n\n"
            + SCHEMA_HELP + "\n\n"
            "Set step_json={'step':'step2a_functions','with_functions':<n_interactors_with_fx>,'total_functions':<n_fx>} and return ONLY JSON."
        ),
    ),

    # 2b — Deeper function discovery
    StepConfig(
        name="step2b_functions",
        model="gpt-5.0",
        deep_research=False,
        reasoning_effort="high",
        use_web_search=True,
        thinking_budget=MAX_THINKING_TOKENS,
        max_output_tokens=MAX_OUTPUT_TOKENS,
        web_search_dynamic_mode=True,
        web_search_dynamic_threshold=DYNAMIC_SEARCH_THRESHOLD,
        expected_columns=["ctx_json", "step_json"],
        system_prompt=None,
        prompt_template=(
            STRICT_GUARDRAILS + "\n\n"
            "KAZI 2b — FIND ADDITIONAL, MISSED FUNCTIONS (no duplicates).\n"
            "YOU ALREADY FOUND: {ctx_json.function_history}\n\n"
            "- Re‑query each interactor with task‑specific search terms (e.g., '<GENE> ataxin-3 deubiquitinates', 'regulates', 'phosphorylates', 'localizes', 'recruits', 'ERAD', 'mitophagy', etc.).\n"
            "- Add only if directly caused by the interaction and supported by primary evidence.\n\n"
            "DUPLICATE HANDLING\n"
            "- Normalize synonyms (e.g., 'ER-associated degradation' → 'ERAD').\n"
            "- If a new paper refines an existing function, merge pmids/evidence and update confidence; do not create a duplicate.\n\n"
            + SCHEMA_HELP + "\n\n"
            "If none found: step_json={'step':'step2b_functions','count':0,'message':'No additional functions found'}. Return ONLY JSON."
        ),
    ),

    # 2b2 — One more function pass
    StepConfig(
        name="step2b2_functions",
        model="gpt-5.0",
        deep_research=False,
        reasoning_effort="high",
        use_web_search=True,
        thinking_budget=MAX_THINKING_TOKENS,
        max_output_tokens=MAX_OUTPUT_TOKENS,
        web_search_dynamic_mode=True,
        web_search_dynamic_threshold=DYNAMIC_SEARCH_THRESHOLD,
        expected_columns=["ctx_json", "step_json"],
        system_prompt=None,
        prompt_template=(
            STRICT_GUARDRAILS + "\n\n"
            "KAZI 2b2 — FINAL FUNCTION DISCOVERY PASS.\n"
            "YOU ALREADY FOUND (history): {ctx_json.function_history}\n\n"
            "- Repeat targeted searches with alternative verbs/synonyms; include contradictory/negative findings only if supported and clearly worded.\n"
            "- Keep only evidenced, non‑duplicate functions.\n\n"
            + SCHEMA_HELP + "\n\n"
            "If none: step_json={'step':'step2b2_functions','count':0,'message':'No additional functions found'}. Return ONLY JSON."
        ),
    ),

    # 2b3 — One more function pass (same as 2b2)
    StepConfig(
        name="step2b3_functions",
        model="gpt-5.0",
        deep_research=False,
        reasoning_effort="high",
        use_web_search=True,
        thinking_budget=MAX_THINKING_TOKENS,
        max_output_tokens=MAX_OUTPUT_TOKENS,
        web_search_dynamic_mode=True,
        web_search_dynamic_threshold=DYNAMIC_SEARCH_THRESHOLD,
        expected_columns=["ctx_json", "step_json"],
        system_prompt=None,
        prompt_template=(
            STRICT_GUARDRAILS + "\n\n"
            "KAZI 2b3 — FINAL FUNCTION DISCOVERY PASS.\n"
            "YOU ALREADY FOUND (history): {ctx_json.function_history}\n\n"
            "- Repeat targeted searches with alternative verbs/synonyms; include contradictory/negative findings only if supported and clearly worded.\n"
            "- Keep only evidenced, non-duplicate functions.\n\n"
            + SCHEMA_HELP + "\n\n"
            "If none: step_json={'step':'step2b3_functions','count':0,'message':'No additional functions found'}. Return ONLY JSON."
        ),
    ),


    # 2c — Validation & normalization
    StepConfig(
        name="step2c_functions",
        model="gpt-5.0",
        deep_research=False,
        reasoning_effort="high",
        use_web_search=True,
        thinking_budget=MAX_THINKING_TOKENS,
        max_output_tokens=MAX_OUTPUT_TOKENS,
        web_search_dynamic_mode=True,
        web_search_dynamic_threshold=DYNAMIC_SEARCH_THRESHOLD,
        expected_columns=["ctx_json", "step_json"],
        system_prompt=None,
        prompt_template=(
            STRICT_GUARDRAILS + "\n\n"
            "KAZI 2c — VALIDATE & NORMALIZE FUNCTIONS.\n"
            "- Ensure EVERY function has PMID/DOI; drop items lacking evidence.\n"
            "- Ensure biological_consequence ≠ cellular_process and reads as a cascade (A→B→C→D).\n"
            "- Deduplicate by normalized function name per (interactor,direction,mechanism_id,arrow). Merge evidence and keep highest confidence.\n"
            "- Attribute functions to the correct protein per direction.\n\n"
            + SCHEMA_HELP + "\n\n"
            "Return ONLY JSON with step_json={'step':'step2c_functions','validated':true}."
        ),
    ),

    # 2e — Make consequences specific (fix vagueness)
    StepConfig(
        name="step2e_fix_consequences",
        model="gpt-5.0",
        deep_research=False,
        reasoning_effort="high",
        use_web_search=True,
        thinking_budget=MAX_THINKING_TOKENS,
        max_output_tokens=MAX_OUTPUT_TOKENS,
        web_search_dynamic_mode=True,
        web_search_dynamic_threshold=DYNAMIC_SEARCH_THRESHOLD,
        expected_columns=["ctx_json", "step_json"],
        system_prompt=None,
        prompt_template=(
            STRICT_GUARDRAILS + "\n\n"
            "KAZI 2e — REWRITE VAGUE biological_consequence INTO SPECIFIC, NAME‑RICH CASCADES.\n"
            "- Use concrete proteins/genes and pathway nodes (e.g., 'HSP70, BiP/GRP78, PERK/eIF2α/ATF4/CHOP').\n"
            "- Keep mechanisms precise; avoid generalities like 'affects immunity' — state the exact module activated/inhibited and downstream outcomes.\n\n"
            "For every function lacking a detailed cascade: search → extract → replace biological_consequence with a stepwise chain using '→'.\n"
            "Return ONLY JSON with step_json={'step':'step2e_fix_consequences','rewritten':<n_fixed>}."
        ),
    ),

    # 2f — Evidence completion (titles, journals, quotes)
    StepConfig(
        name="step2f_add_evidence",
        model="gpt-5.0",
        deep_research=False,
        reasoning_effort="high",
        use_web_search=True,
        thinking_budget=MAX_THINKING_TOKENS,
        max_output_tokens=MAX_OUTPUT_TOKENS,
        web_search_dynamic_mode=True,
        web_search_dynamic_threshold=DYNAMIC_SEARCH_THRESHOLD,
        expected_columns=["ctx_json", "step_json"],
        system_prompt=None,
        prompt_template=(
            STRICT_GUARDRAILS + "\n\n"
            "KAZI 2f — COMPLETE EVIDENCE BLOCKS (titles/journals/quotes).\n"
            "- For each function/interactor lacking full metadata, search and fill pmid, doi (if present), exact paper_title, authors, journal, year, and a short supporting quote.\n"
            "- If an existing citation looks wrong or mismatched, REPLACE it with a verified one.\n"
            "- If you cannot verify a source, REMOVE the claim/function rather than guess.\n\n"
            + SCHEMA_HELP + "\n\n"
            "Return ONLY JSON with step_json={'step':'step2f_add_evidence','completed':<n_completed>,'removed':<n_removed>}."
        ),
    ),

    # 2g — Final ruthless quality enforcement (search ON)
    StepConfig(
        name="step2g_final_enforcement",
        model="gpt-5.0",
        deep_research=False,
        reasoning_effort="high",
        use_web_search=True,  # keep ON for re-verification
        thinking_budget=MAX_THINKING_TOKENS,
        max_output_tokens=MAX_OUTPUT_TOKENS,
        web_search_dynamic_mode=True,
        web_search_dynamic_threshold=DYNAMIC_SEARCH_THRESHOLD,
        expected_columns=["ctx_json", "step_json"],
        system_prompt=None,
        prompt_template=(
            STRICT_GUARDRAILS + "\n\n"
            "KAZI 2g — FINAL RUTHLESS QUALITY ENFORCEMENT.\n"
            "- Re‑check a sample of claims with fresh searches; if any contradiction is found, fix or drop.\n"
            "- Ensure every item is directional, attributed, and evidence‑backed with correct metadata.\n"
            "- Ensure no duplicated functions, no vague consequences, and consistent HGNC symbols.\n\n"
            "Return ONLY JSON with step_json={'step':'step2g_final_enforcement','status':'clean'}."
        ),
    ),

    # 3 — Snapshot (handled locally by runner)
    StepConfig(
        name="step3_snapshot",
        model="gpt-5.0",
        deep_research=False,
        reasoning_effort="high",
        use_web_search=False,
        thinking_budget=MAX_THINKING_TOKENS,
        max_output_tokens=MAX_OUTPUT_TOKENS,
        expected_columns=["ctx_json", "snapshot_json", "ndjson", "step_json"],
        system_prompt=None,
        prompt_template=(
            "This step is handled locally without model call."
        ),
    ),
]
