from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from app.content import normalize_bundle, normalize_guide
from app.providers import CompatibleProvider

JD = """机械结构设计工程师
负责小型自动化设备的支架与装配结构设计，输出二维工程图和零件清单。
评审装配干涉、加工可行性与维护空间，并解释设计取舍。
要求熟悉 CAD，能用图纸和可检查的资料沟通方案。"""
MATERIAL = """虚构学生：机械工程本科，2026 年毕业。
课程设计：为桌面分拣装置绘制支架装配图和零件清单；个人负责支架建模、螺栓连接检查与图纸标注，最后提交课程报告。
技能：使用 AutoCAD 绘图；未进行有限元仿真、工厂加工或实物载荷测试。"""


def load_settings(path: Path) -> None:
    values = {}
    for line in path.read_text().splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip().strip('"\'')
    for new, old in {
        "COACH_API_BASE_URL": "LLM_API_BASE_URL",
        "COACH_API_KEY": "LLM_API_KEY",
        "COACH_MODEL": "LLM_MODEL_STRONG",
    }.items():
        if values.get(old):
            os.environ[new] = values[old]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--env-file", type=Path)
    parser.add_argument("--reuse-bundle", action="store_true")
    args = parser.parse_args()
    if args.env_file:
        load_settings(args.env_file)
    provider = CompatibleProvider()
    raw = json.loads(Path("data/live-raw.json").read_text()) if args.reuse_bundle else provider.bundle(JD, MATERIAL)
    if not args.reuse_bundle:
        Path("data/live-raw.json").write_text(json.dumps(raw, ensure_ascii=False, indent=2))
    normalized = normalize_bundle(raw, JD, MATERIAL, True)
    guides = {}
    for kind in ("job", "blueprint"):
        guide_raw = provider.guide(kind, normalized["resumes"][kind], normalized["plans"], JD)
        guides[kind] = normalize_guide(guide_raw, normalized["resumes"][kind], kind)
    output = {"fictional": True, "provider": provider.name, "model": provider.model, "jd": JD, "material": MATERIAL, **normalized, "guides": guides}
    path = Path("data/live-evaluation.json")
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps(output, ensure_ascii=False, indent=2))
    print(json.dumps({"status": "validated", "model": provider.model, "plan_count": len(normalized["plans"]), "job_items": len(normalized["resumes"]["job"]["items"]), "blueprint_items": len(normalized["resumes"]["blueprint"]["items"]), "job_guide_entries": len(guides["job"]), "blueprint_guide_entries": len(guides["blueprint"]), "artifact": str(path)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
