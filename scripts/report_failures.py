"""Open a GitHub issue for sources that failed 3 runs in a row; close it when they recover.
Uses the gh CLI (preinstalled on GitHub runners) with GH_TOKEN."""
import json
import subprocess
import sys

THRESHOLD = 3
LABEL = "source-broken"


def gh(*args) -> str:
    return subprocess.run(["gh", *args], check=True, capture_output=True, text=True).stdout


def main(index_path: str) -> None:
    index = json.load(open(index_path, encoding="utf-8"))
    try:
        gh("label", "create", LABEL, "--color", "d73a4a", "--description", "A vendor source stopped resolving")
    except subprocess.CalledProcessError:
        pass  # exists
    open_issues = {i["title"]: i["number"] for i in json.loads(gh("issue", "list", "--label", LABEL, "--state", "open", "--json", "title,number"))}
    for p in index["products"]:
        st = p.get("status") or {}
        title = f"Source '{p['id']}' is failing"
        if int(st.get("consecutiveFailures") or 0) >= THRESHOLD and title not in open_issues:
            body = (f"The catalog build could not check **{p['name']}** for {st['consecutiveFailures']} runs in a row.\n\n"
                    f"```\n{st.get('error')}\n```\n\nThe vendor probably changed its download page or API. "
                    f"Update `src/admxhub/sources/{p['id']}.yaml`; test with `admxhub check --live {p['id']}`.")
            gh("issue", "create", "--title", title, "--label", LABEL, "--body", body)
            print(f"opened issue for {p['id']}")
        elif not st.get("error") and title in open_issues:
            gh("issue", "close", str(open_issues[title]), "--comment", "The source resolves again.")
            print(f"closed issue for {p['id']}")


if __name__ == "__main__":
    main(sys.argv[1])
