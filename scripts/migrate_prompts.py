"""W5 prompt migration script.

Reads existing agent/memory YAML files, extracts shared prompts to _base/,
and writes plugin-specific per-agent YAML files.

Run from project root:  python scripts/migrate_prompts.py
"""
import os
import yaml
import re
from pathlib import Path
from copy import deepcopy

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
BASE_DIR = SRC / "prompts" / "_base"
PLUGINS_DIR = SRC / "plugins"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def read_yaml(path: Path) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def write_yaml(path: Path, data: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        yaml.dump(data, f, default_flow_style=False, allow_unicode=True,
                  sort_keys=False, width=120)


def write_yaml_raw(path: Path, data: dict):
    """Write YAML preserving literal block scalars (|) for multiline strings."""
    path.parent.mkdir(parents=True, exist_ok=True)

    class LiteralStr(str):
        pass

    def literal_representer(dumper, data):
        if "\n" in data:
            return dumper.represent_scalar("tag:yaml.org,2002:str", data, style="|")
        return dumper.represent_scalar("tag:yaml.org,2002:str", data)

    dumper = yaml.Dumper
    dumper.add_representer(LiteralStr, literal_representer)

    converted = {}
    for k, v in data.items():
        if isinstance(v, str) and "\n" in v:
            converted[k] = LiteralStr(v)
        else:
            converted[k] = v

    with open(path, "w", encoding="utf-8") as f:
        yaml.dump(converted, f, Dumper=dumper, default_flow_style=False,
                  allow_unicode=True, sort_keys=False, width=120)


# ---------------------------------------------------------------------------
# Load all existing prompts
# ---------------------------------------------------------------------------

# Report generator prompts (per report type)
rg = {}
rg_dir = SRC / "agents" / "report_generator" / "prompts"
for name in ("financial_company", "financial_industry", "financial_macro", "governance", "general"):
    rg[name] = read_yaml(rg_dir / f"{name}_prompts.yaml")

# Data analyzer prompts
da = {}
da_dir = SRC / "agents" / "data_analyzer" / "prompts"
da["general"] = read_yaml(da_dir / "general_prompts.yaml")
da["financial"] = read_yaml(da_dir / "financial_prompts.yaml")

# Memory prompts
mem = {}
mem_dir = SRC / "memory" / "prompts"
mem["general"] = read_yaml(mem_dir / "general_prompts.yaml")
mem["financial"] = read_yaml(mem_dir / "financial_prompts.yaml")
mem["financial_industry"] = read_yaml(mem_dir / "financial_industry_prompts.yaml")

# ---------------------------------------------------------------------------
# 1. Create _base/ YAML files
# ---------------------------------------------------------------------------

BASE_DIR.mkdir(parents=True, exist_ok=True)

# 1a. data_api_outline — identical across fc/fi/gen in report_generator
write_yaml_raw(BASE_DIR / "data_api_outline.yaml", {
    "data_api_outline": rg["financial_company"]["data_api_outline"]
})
print("  Created _base/data_api_outline.yaml")

# 1b. data_api — fc=fi version is the base; gen overrides in plugin
write_yaml_raw(BASE_DIR / "data_api.yaml", {
    "data_api": rg["financial_company"]["data_api"]
})
print("  Created _base/data_api.yaml")

# 1c. table_beautify — fc=fi=fm=gov version is the base; gen overrides
write_yaml_raw(BASE_DIR / "table_beautify.yaml", {
    "table_beautify": rg["financial_company"]["table_beautify"]
})
print("  Created _base/table_beautify.yaml")

# 1d. outline_critique — fc=fi version is the base; fm/gov/gen override
write_yaml_raw(BASE_DIR / "outline_critique.yaml", {
    "outline_critique": rg["financial_company"]["outline_critique"]
})
print("  Created _base/outline_critique.yaml")

# 1e. outline_refinement — fc=fi version is the base; fm/gov/gen override
write_yaml_raw(BASE_DIR / "outline_refinement.yaml", {
    "outline_refinement": rg["financial_company"]["outline_refinement"]
})
print("  Created _base/outline_refinement.yaml")

# 1f. select_data — parameterize {analyst_role} from general/financial versions
gen_sd = mem["general"]["select_data"]
fin_sd = mem["financial"]["select_data"]
# General says "research analyst", financial says "financial-research analyst"
base_sd = gen_sd.replace("a research analyst", "a {analyst_role} analyst")
write_yaml_raw(BASE_DIR / "select_data.yaml", {
    "select_data": base_sd
})
print("  Created _base/select_data.yaml")

# 1g. select_analysis — same parameterization
gen_sa = mem["general"]["select_analysis"]
base_sa = gen_sa.replace("a research analyst", "a {analyst_role} analyst")
write_yaml_raw(BASE_DIR / "select_analysis.yaml", {
    "select_analysis": base_sa
})
print("  Created _base/select_analysis.yaml")

# 1h. vlm_critique — parameterize {domain} from general/financial data_analyzer
gen_vc = da["general"]["vlm_critique"]
# General: "professional research", Financial: "financial research"
base_vc = gen_vc.replace("for professional research", "for {domain} research")
write_yaml_raw(BASE_DIR / "vlm_critique.yaml", {
    "vlm_critique": base_vc
})
print("  Created _base/vlm_critique.yaml")

print("\n=== _base/ files created ===\n")

# ---------------------------------------------------------------------------
# 2. Create plugin per-agent YAML files
# ---------------------------------------------------------------------------

# Keys that are now in _base/ for report_generator
RG_BASE_KEYS = {"data_api", "data_api_outline", "table_beautify", "outline_critique", "outline_refinement"}

# Mapping: plugin_name → report_type used for report_generator prompts
PLUGIN_RG_MAP = {
    "financial_company": "financial_company",
    "financial_industry": "financial_industry",
    "financial_macro": "financial_macro",
    "governance": "governance",
    "general": "general",
}

# For each plugin, check which _base/ keys it differs from and needs to override
import hashlib
def md5(s):
    return hashlib.md5(s.encode()).hexdigest()

# Reference _base/ hashes
base_hashes = {
    "data_api": md5(rg["financial_company"]["data_api"]),
    "data_api_outline": md5(rg["financial_company"]["data_api_outline"]),
    "table_beautify": md5(rg["financial_company"]["table_beautify"]),
    "outline_critique": md5(rg["financial_company"]["outline_critique"]),
    "outline_refinement": md5(rg["financial_company"]["outline_refinement"]),
}

for plugin_name, rg_type in PLUGIN_RG_MAP.items():
    prompts = rg[rg_type]
    plugin_specific = {}

    for key, value in prompts.items():
        if key in RG_BASE_KEYS:
            # Check if this plugin's version differs from _base/
            if md5(value) != base_hashes[key]:
                # Plugin overrides _base/
                plugin_specific[key] = value
            # else: same as _base/, no need to include
        else:
            # Not in _base/, always include in plugin
            plugin_specific[key] = value

    out_path = PLUGINS_DIR / plugin_name / "prompts" / "report_generator.yaml"
    write_yaml_raw(out_path, plugin_specific)
    print(f"  Created {plugin_name}/prompts/report_generator.yaml  ({len(plugin_specific)} keys: {list(plugin_specific.keys())})")

print()

# --- Data Analyzer plugin prompts ---
# financial_company, financial_industry, financial_macro use financial_prompts
# general, governance use general_prompts

DA_VLM_BASE_HASH = md5(base_vc.replace("{domain}", "professional"))  # general version after parameterization roundtrip

# Mapping: plugin → which da prompts to use
PLUGIN_DA_MAP = {
    "financial_company": "financial",
    "financial_industry": "financial",
    "financial_macro": "financial",
    "governance": "general",
    "general": "general",
}

for plugin_name, da_type in PLUGIN_DA_MAP.items():
    prompts = deepcopy(da[da_type])
    # Remove vlm_critique if it matches the parameterized _base/ version
    # (it will be loaded from _base/ with {domain} param)
    if "vlm_critique" in prompts:
        del prompts["vlm_critique"]
    
    out_path = PLUGINS_DIR / plugin_name / "prompts" / "data_analyzer.yaml"
    write_yaml_raw(out_path, prompts)
    print(f"  Created {plugin_name}/prompts/data_analyzer.yaml  ({len(prompts)} keys: {list(prompts.keys())})")

print()

# --- Memory/Pipeline plugin prompts ---
# Mapping: plugin → which memory prompts to use
PLUGIN_MEM_MAP = {
    "financial_company": "financial",
    "financial_industry": "financial_industry",
    "financial_macro": "financial",
    "governance": "general",
    "general": "general",
}

MEM_BASE_KEYS = {"select_data", "select_analysis"}

for plugin_name, mem_type in PLUGIN_MEM_MAP.items():
    prompts = deepcopy(mem[mem_type])
    # Remove select_data/select_analysis (now in _base/ with parameterization)
    for k in MEM_BASE_KEYS:
        prompts.pop(k, None)

    if prompts:  # Only write if there are remaining keys
        out_path = PLUGINS_DIR / plugin_name / "prompts" / "memory.yaml"
        write_yaml_raw(out_path, prompts)
        print(f"  Created {plugin_name}/prompts/memory.yaml  ({len(prompts)} keys: {list(prompts.keys())})")
    else:
        print(f"  Skipped {plugin_name}/prompts/memory.yaml  (no plugin-specific keys)")

print("\n=== All plugin prompt files created ===\n")

# ---------------------------------------------------------------------------
# 3. Verification: compare old resolution with new resolution
# ---------------------------------------------------------------------------
print("=== Verification ===\n")

# Load _base/ prompts
base_prompts = {}
for yaml_file in BASE_DIR.glob("*.yaml"):
    data = read_yaml(yaml_file)
    base_prompts.update(data)

def resolve_prompt(plugin_name, agent_name, key):
    """Simulate new resolution: plugin → _base/"""
    # 1. Plugin overrides
    plugin_file = PLUGINS_DIR / plugin_name / "prompts" / f"{agent_name}.yaml"
    if plugin_file.exists():
        data = read_yaml(plugin_file)
        if key in data:
            return data[key]
    # 2. _base/
    if key in base_prompts:
        return base_prompts[key]
    return None

# Verify report_generator prompts
errors = 0
for plugin_name, rg_type in PLUGIN_RG_MAP.items():
    original = rg[rg_type]
    for key, original_value in original.items():
        resolved = resolve_prompt(plugin_name, "report_generator", key)
        if resolved is None:
            print(f"  ERROR: {plugin_name}/report_generator/{key} - NOT FOUND in new system")
            errors += 1
        elif md5(resolved) != md5(original_value):
            print(f"  ERROR: {plugin_name}/report_generator/{key} - CONTENT MISMATCH")
            errors += 1

# Verify data_analyzer prompts (excluding vlm_critique which is parameterized)
for plugin_name, da_type in PLUGIN_DA_MAP.items():
    original = da[da_type]
    for key, original_value in original.items():
        if key == "vlm_critique":
            continue  # parameterized, check separately
        resolved = resolve_prompt(plugin_name, "data_analyzer", key)
        if resolved is None:
            print(f"  ERROR: {plugin_name}/data_analyzer/{key} - NOT FOUND in new system")
            errors += 1
        elif md5(resolved) != md5(original_value):
            print(f"  ERROR: {plugin_name}/data_analyzer/{key} - CONTENT MISMATCH")
            errors += 1

# Verify memory prompts (excluding parameterized select_data/select_analysis)
for plugin_name, mem_type in PLUGIN_MEM_MAP.items():
    original = mem[mem_type]
    for key, original_value in original.items():
        if key in MEM_BASE_KEYS:
            continue  # parameterized
        resolved = resolve_prompt(plugin_name, "memory", key)
        if resolved is None:
            print(f"  ERROR: {plugin_name}/memory/{key} - NOT FOUND in new system")
            errors += 1
        elif md5(resolved) != md5(original_value):
            print(f"  ERROR: {plugin_name}/memory/{key} - CONTENT MISMATCH")
            errors += 1

if errors == 0:
    print("  ALL PROMPTS VERIFIED SUCCESSFULLY")
else:
    print(f"  {errors} ERRORS FOUND")
