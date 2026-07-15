from __future__ import annotations

import argparse
import importlib
import json
from pathlib import Path

IMPORTS = {
    "numpy": "numpy",
    "openpyxl": "openpyxl",
    "python-docx": "docx",
    "matplotlib": "matplotlib",
    "pandas": "pandas",
    "python-pptx": "pptx",
    "pypdf": "pypdf",
    "pdfplumber": "pdfplumber",
    "reportlab": "reportlab",
    "Pillow": "PIL",
    "tabulate": "tabulate",
}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=Path("assistant-pydeps-smoke.png"))
    args = parser.parse_args()

    versions: dict[str, str] = {}
    for package_name, module_name in IMPORTS.items():
        module = importlib.import_module(module_name)
        versions[package_name] = str(getattr(module, "__version__", "installed"))

    import matplotlib

    matplotlib.use("Agg")
    from matplotlib import font_manager
    from matplotlib import pyplot as plt

    font_path = font_manager.findfont("WenQuanYi Zen Hei", fallback_to_default=False)
    plt.rcParams["font.sans-serif"] = ["WenQuanYi Zen Hei"]
    plt.rcParams["axes.unicode_minus"] = False

    args.output.parent.mkdir(parents=True, exist_ok=True)
    fig, ax = plt.subplots(figsize=(4, 3), dpi=120)
    ax.bar(["一月", "二月", "三月"], [3, 5, 4], color=["#2563eb", "#16a34a", "#dc2626"])
    ax.set_title("中文字体图表 smoke")
    ax.set_ylabel("数量")
    fig.tight_layout()
    fig.savefig(args.output)
    plt.close(fig)

    print(
        json.dumps(
            {
                "imports": versions,
                "font": font_path,
                "output": str(args.output),
            },
            ensure_ascii=False,
            sort_keys=True,
    )
        )


if __name__ == "__main__":
    main()
