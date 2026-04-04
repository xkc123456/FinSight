"""Verify new PromptLoader resolves correctly for all agents and report types."""
import sys
sys.path.insert(0, '.')

from src.utils.prompt_loader import PromptLoader, get_prompt_loader

errors = 0

def check(label, condition):
    global errors
    if condition:
        print(f"  OK: {label}")
    else:
        print(f"  FAIL: {label}")
        errors += 1


# ---- Test 1: report_generator ----
print("=== Test 1: report_generator ===")
for rt in ["financial_company", "financial_industry", "financial_macro", "governance", "general"]:
    PromptLoader.clear_cache()
    loader = get_prompt_loader("report_generator", report_type=rt)
    keys = loader.list_available_prompts()
    check(f"{rt} has table_beautify", "table_beautify" in keys)
    check(f"{rt} has section_writing", "section_writing" in keys)
    check(f"{rt} has data_api_outline", "data_api_outline" in keys)
    check(f"{rt} has outline_critique", "outline_critique" in keys)
    check(f"{rt} has outline_refinement", "outline_refinement" in keys)
    check(f"{rt} has abstract", "abstract" in keys)
    check(f"{rt} has title_generation", "title_generation" in keys)

    # table_beautify should be loadable
    tb = loader.get_prompt("table_beautify")
    check(f"{rt} table_beautify is non-empty", tb is not None and len(tb) > 50)

print()

# ---- Test 2: memory prompts ----
print("=== Test 2: memory ===")
for rt in ["general", "financial_company", "financial_industry"]:
    PromptLoader.clear_cache()
    loader = get_prompt_loader("memory", report_type=rt)
    keys = loader.list_available_prompts()
    check(f"{rt} has select_data", "select_data" in keys)
    check(f"{rt} has select_analysis", "select_analysis" in keys)
    check(f"{rt} has generate_task", "generate_task" in keys)

    # select_data should have {analyst_role} parameter
    sd_raw = loader.prompts.get("select_data", "")
    check(f"{rt} select_data has analyst_role placeholder", "{analyst_role}" in sd_raw)

print()

# ---- Test 3: data_analyzer ----
print("=== Test 3: data_analyzer ===")
for rt in ["financial_company", "general"]:
    PromptLoader.clear_cache()
    loader = get_prompt_loader("data_analyzer", report_type=rt)
    keys = loader.list_available_prompts()
    check(f"{rt} has data_analysis", "data_analysis" in keys)
    check(f"{rt} has vlm_critique", "vlm_critique" in keys)
    check(f"{rt} has draw_chart", "draw_chart" in keys)

    # vlm_critique should have {domain} parameter
    vc_raw = loader.prompts.get("vlm_critique", "")
    check(f"{rt} vlm_critique has domain placeholder", "{domain}" in vc_raw)

print()

# ---- Test 4: search_agent (agent-dir fallback) ----
print("=== Test 4: search_agent ===")
PromptLoader.clear_cache()
loader = get_prompt_loader("search_agent", report_type="general")
keys = loader.list_available_prompts()
check("search_agent has deep_search", "deep_search" in keys)
ds = loader.get_prompt("deep_search")
check("deep_search is non-empty", ds is not None and len(ds) > 50)

print()

# ---- Test 5: data_collector (agent-dir fallback) ----
print("=== Test 5: data_collector ===")
PromptLoader.clear_cache()
loader = get_prompt_loader("data_collector", report_type="financial_company")
keys = loader.list_available_prompts()
check("data_collector has data_collect", "data_collect" in keys)

print()

# ---- Test 6: Content verification - compare old vs new ----
print("=== Test 6: Content hash verification ===")
import yaml, hashlib

def md5(s):
    return hashlib.md5(s.encode()).hexdigest()

# Load old prompts
old_rg = {}
for name in ["financial_company", "financial_industry", "financial_macro", "governance", "general"]:
    with open(f"src/agents/report_generator/prompts/{name}_prompts.yaml", "r", encoding="utf-8") as f:
        old_rg[name] = yaml.safe_load(f) or {}

for rt, old_prompts in old_rg.items():
    PromptLoader.clear_cache()
    loader = get_prompt_loader("report_generator", report_type=rt)
    for key, old_val in old_prompts.items():
        new_val = loader.prompts.get(key)
        if new_val is None:
            check(f"{rt}/{key} found", False)
        else:
            check(f"{rt}/{key} content matches", md5(new_val) == md5(old_val))

# Data analyzer
old_da = {}
for name, fname in [("financial_company", "financial"), ("general", "general")]:
    with open(f"src/agents/data_analyzer/prompts/{fname}_prompts.yaml", "r", encoding="utf-8") as f:
        old_da[name] = yaml.safe_load(f) or {}

da_plugin_map = {"financial_company": "financial_company", "general": "general"}
for rt, old_prompts in old_da.items():
    PromptLoader.clear_cache()
    loader = get_prompt_loader("data_analyzer", report_type=rt)
    for key, old_val in old_prompts.items():
        new_val = loader.prompts.get(key)
        if key == "vlm_critique":
            # Parameterized - check it exists and has the placeholder
            check(f"da/{rt}/{key} exists with domain param", new_val is not None and "{domain}" in new_val)
        elif new_val is None:
            check(f"da/{rt}/{key} found", False)
        else:
            check(f"da/{rt}/{key} content matches", md5(new_val) == md5(old_val))

print()
print(f"{'ALL TESTS PASSED' if errors == 0 else f'{errors} FAILURES'}")
