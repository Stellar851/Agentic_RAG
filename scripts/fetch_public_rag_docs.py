"""
从公开 GitHub 开源官方文档仓库批量拉取高质量中文长文档，用于扩充 RAG 知识库测试集。
包含：国家法律合规原文 (LawRefBook/Laws)、TiDB 官方中文架构文档 (pingcap/docs-cn)、
Kubernetes 官方中文文档 (kubernetes/website) 与 FastAPI 官方中文文档 (fastapi/fastapi)。
"""

import os
import re
import sys
import math
from urllib.parse import quote
from typing import Dict, List, Optional, Tuple

import httpx

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TARGET_DIR = os.path.join(
    PROJECT_ROOT, "测试集", "测试文档", "03_真实公开权威长文档扩充包"
)

# 每个条目: (out_filename, repo, preferred_branches, candidate_paths, tree_keywords)
DOC_SPECS: List[Tuple[str, str, List[str], List[str], List[str]]] = [
    # 1. 国家法律与合规原文 (LawRefBook/Laws)
    (
        "01_法律合规_中华人民共和国网络安全法.md",
        "LawRefBook/Laws",
        ["master", "main"],
        ["经济法/网络安全法.md", "行政法/网络安全法.md"],
        ["网络安全法.md"],
    ),
    (
        "02_法律合规_中华人民共和国数据安全法.md",
        "LawRefBook/Laws",
        ["master", "main"],
        ["经济法/数据安全法.md", "行政法/数据安全法.md"],
        ["数据安全法.md"],
    ),
    (
        "03_法律合规_中华人民共和国个人信息保护法.md",
        "LawRefBook/Laws",
        ["master", "main"],
        ["民法典/个人信息保护法.md", "民商法/个人信息保护法.md", "经济法/个人信息保护法.md"],
        ["个人信息保护法.md"],
    ),
    (
        "04_法律合规_中华人民共和国电子商务法.md",
        "LawRefBook/Laws",
        ["master", "main"],
        ["经济法/电子商务法.md", "民商法/电子商务法.md"],
        ["电子商务法.md"],
    ),
    # 2. TiDB 官方中文架构文档 (pingcap/docs-cn)
    (
        "05_TiDB官方_整体架构.md",
        "pingcap/docs-cn",
        ["master", "main"],
        ["tidb-architecture.md"],
        ["tidb-architecture.md"],
    ),
    (
        "06_TiDB官方_分布式事务概览.md",
        "pingcap/docs-cn",
        ["master", "main"],
        ["transaction-overview.md"],
        ["transaction-overview.md"],
    ),
    (
        "07_TiDB官方_悲观事务模型.md",
        "pingcap/docs-cn",
        ["master", "main"],
        ["pessimistic-transaction.md"],
        ["pessimistic-transaction.md"],
    ),
    (
        "08_TiDB官方_乐观事务模型.md",
        "pingcap/docs-cn",
        ["master", "main"],
        ["optimistic-transaction.md"],
        ["optimistic-transaction.md"],
    ),
    (
        "09_TiDB官方_高可用与容灾概览.md",
        "pingcap/docs-cn",
        ["master", "main"],
        ["high-availability-overview.md", "br/backup-and-restore-overview.md"],
        ["high-availability", "disaster-recovery", "backup-and-restore-overview.md"],
    ),
    # 3. Kubernetes 官方中文文档 (kubernetes/website)
    (
        "10_K8s官方_核心组件架构.md",
        "kubernetes/website",
        ["main", "master"],
        ["content/zh-cn/docs/concepts/overview/components.md"],
        ["content/zh-cn/docs/concepts/overview/components.md"],
    ),
    (
        "11_K8s官方_Pod生命周期.md",
        "kubernetes/website",
        ["main", "master"],
        ["content/zh-cn/docs/concepts/workloads/pods/pod-lifecycle.md"],
        ["content/zh-cn/docs/concepts/workloads/pods/pod-lifecycle.md"],
    ),
    (
        "12_K8s官方_Service服务网络.md",
        "kubernetes/website",
        ["main", "master"],
        ["content/zh-cn/docs/concepts/services-networking/service.md"],
        ["content/zh-cn/docs/concepts/services-networking/service.md"],
    ),
    (
        "13_K8s官方_调度器kube-scheduler.md",
        "kubernetes/website",
        ["main", "master"],
        ["content/zh-cn/docs/concepts/scheduling-eviction/kube-scheduler.md"],
        ["content/zh-cn/docs/concepts/scheduling-eviction/kube-scheduler.md"],
    ),
    (
        "14_K8s官方_RBAC鉴权规范.md",
        "kubernetes/website",
        ["main", "master"],
        ["content/zh-cn/docs/reference/access-authn-authz/rbac.md"],
        ["content/zh-cn/docs/reference/access-authn-authz/rbac.md"],
    ),
    # 4. FastAPI 官方中文文档 (fastapi/fastapi)
    (
        "15_FastAPI官方_并发与Async指南.md",
        "fastapi/fastapi",
        ["master", "main"],
        ["docs/zh/docs/async.md"],
        ["docs/zh/docs/async.md"],
    ),
    (
        "16_FastAPI官方_安全OAuth2与JWT认证.md",
        "fastapi/fastapi",
        ["master", "main"],
        ["docs/zh/docs/tutorial/security/oauth2-jwt.md", "docs/zh/docs/tutorial/security/first-steps.md"],
        ["docs/zh/docs/tutorial/security/oauth2-jwt.md", "docs/zh/docs/tutorial/security/"],
    ),
    (
        "17_FastAPI官方_SQL关系型数据库.md",
        "fastapi/fastapi",
        ["master", "main"],
        ["docs/zh/docs/tutorial/sql-databases.md", "docs/zh/docs/advanced/async-sql-databases.md"],
        ["docs/zh/docs/tutorial/sql-databases.md", "docs/zh/docs/advanced/async-sql-databases.md"],
    ),
    (
        "18_FastAPI官方_依赖注入核心机制.md",
        "fastapi/fastapi",
        ["master", "main"],
        ["docs/zh/docs/tutorial/dependencies/index.md"],
        ["docs/zh/docs/tutorial/dependencies/index.md", "docs/zh/docs/tutorial/dependencies/"],
    ),
    (
        "19_FastAPI官方_中间件与请求治理.md",
        "fastapi/fastapi",
        ["master", "main"],
        ["docs/zh/docs/tutorial/middleware.md", "docs/zh/docs/tutorial/background-tasks.md"],
        ["docs/zh/docs/tutorial/middleware.md", "docs/zh/docs/tutorial/background-tasks.md"],
    ),
]

_TREE_CACHE: Dict[str, Tuple[str, List[str]]] = {}


def clean_markdown(raw_text: str, repo: str, rel_path: str) -> str:
    """剥离站点头部的 YAML frontmatter、HTML 注释与 Hugo 模板短代码，保留纯正知识文本。"""
    text = raw_text.lstrip("\ufeff")
    # 1. 剥离顶部 YAML frontmatter
    if text.startswith("---"):
        text = re.sub(r"^---\s*\r?\n[\s\S]*?\r?\n---\s*\r?\n?", "", text, count=1)

    # 2. 剥离 HTML 注释
    text = re.sub(r"<!--[\s\S]*?-->", "", text)

    # 3. 剥离 Hugo / MkDocs 常见短代码与选项卡标签，保留内部文字
    text = re.sub(r"\{\{[<%]\s*/?(?:note|tip|warning|caution|important|tabs?|tab|codenew|glossary_tooltip|feature-state|copyable)[^}>%]*[>%]\}\}", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\{\{[<%][^}>%]*[>%]\}\}", "", text)

    # 4. 清理多余连续空行
    text = re.sub(r"\n{3,}", "\n\n", text).strip()

    header = f"> 来源：GitHub 开源官方文档 (`{repo}/{rel_path}`)\n\n"
    return header + text + "\n"


def _is_valid_markdown(text: str) -> bool:
    if not text:
        return False
    low = text[:400].lower()
    if "<!doctype html" in low or "<html" in low:
        return False
    return len(text.strip()) >= 800


def _get_repo_tree(client: httpx.Client, repo: str, preferred_branches: List[str]) -> Optional[Tuple[str, List[str]]]:
    if repo in _TREE_CACHE:
        return _TREE_CACHE[repo]
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "RAG-Benchmark-Fetcher/1.0"}
    for branch in preferred_branches:
        api_url = f"https://api.github.com/repos/{repo}/git/trees/{branch}?recursive=1"
        try:
            resp = client.get(api_url, headers=headers)
            if resp.status_code == 200:
                tree = resp.json().get("tree", [])
                paths = [item["path"] for item in tree if item.get("type") == "blob" and item.get("path", "").endswith(".md")]
                _TREE_CACHE[repo] = (branch, paths)
                return branch, paths
        except Exception:
            continue
    return None


def _fetch_raw(client: httpx.Client, repo: str, branch: str, rel_path: str) -> Optional[str]:
    encoded_path = "/".join(quote(seg, safe="") for seg in rel_path.split("/"))
    url = f"https://raw.githubusercontent.com/{repo}/{branch}/{encoded_path}"
    try:
        resp = client.get(url, headers={"User-Agent": "RAG-Benchmark-Fetcher/1.0"})
        if resp.status_code == 200 and _is_valid_markdown(resp.text):
            return resp.text
    except Exception:
        return None
    return None


def main() -> int:
    os.makedirs(TARGET_DIR, exist_ok=True)
    print(f"📂 目标存储目录: {TARGET_DIR}")
    print("=" * 84)

    results = []
    failed = []

    with httpx.Client(follow_redirects=True, timeout=30.0) as client:
        for out_name, repo, branches, candidates, keywords in DOC_SPECS:
            fetched_text = None
            hit_branch = None
            hit_path = None

            # 1. 先直连尝试已知路径
            for branch in branches:
                for rel_path in candidates:
                    txt = _fetch_raw(client, repo, branch, rel_path)
                    if txt:
                        fetched_text = txt
                        hit_branch = branch
                        hit_path = rel_path
                        break
                if fetched_text:
                    break

            # 2. 若路径变更，利用 GitHub Tree API 自动模糊定位
            if not fetched_text:
                tree_info = _get_repo_tree(client, repo, branches)
                if tree_info:
                    t_branch, all_paths = tree_info
                    matched_path = None
                    for kw in keywords:
                        # 优先精确后缀匹配，其次包含匹配
                        exact_hits = [p for p in all_paths if p.endswith(kw) and ("修正" not in p)]
                        if exact_hits:
                            matched_path = exact_hits[0]
                            break
                        fuzzy_hits = [p for p in all_paths if kw in p and ("修正" not in p)]
                        if fuzzy_hits:
                            matched_path = fuzzy_hits[0]
                            break
                    if matched_path:
                        txt = _fetch_raw(client, repo, t_branch, matched_path)
                        if txt:
                            fetched_text = txt
                            hit_branch = t_branch
                            hit_path = matched_path

            if not fetched_text or not hit_path:
                failed.append((out_name, repo))
                print(f"❌ 失败: {out_name} (来源: {repo})")
                continue

            cleaned = clean_markdown(fetched_text, repo, hit_path)
            if len(cleaned) < 800:
                failed.append((out_name, repo))
                print(f"⚠️ 内容过短被跳过: {out_name} ({len(cleaned)} 字符)")
                continue

            out_path = os.path.join(TARGET_DIR, out_name)
            with open(out_path, "w", encoding="utf-8") as f:
                f.write(cleaned)

            byte_size = os.path.getsize(out_path)
            char_cnt = len(cleaned)
            est_chunks = max(1, math.ceil(char_cnt / 500))
            results.append((out_name, byte_size, char_cnt, est_chunks, f"{repo}@{hit_branch}:{hit_path}"))
            print(f"✅ 已保存: {out_name:42s} | {byte_size/1024:5.1f} KB | {char_cnt:6d} 字 | ~{est_chunks:2d} 切片")

    total_bytes = sum(r[1] for r in results)
    total_chars = sum(r[2] for r in results)
    total_chunks = sum(r[3] for r in results)

    print("=" * 84)
    print(f"📊 下载完成：成功 {len(results)} / {len(DOC_SPECS)} 篇 | 失败 {len(failed)} 篇")
    print(f"📦 总体积：{total_bytes/1024:.1f} KB | 总字符：{total_chars:,} 字 | 预估新增切片：约 {total_chunks} 个")
    if failed:
        print("⚠️ 失败清单：", ", ".join(x[0] for x in failed))
    print("=" * 84)

    return 0 if len(results) >= 15 else 1


if __name__ == "__main__":
    sys.exit(main())
