from typing import List, Set, Tuple

# Set of disallowed flags that take a subsequent argument value
DISALLOWED_WITH_VALUE: Set[str] = {
    "--proxy",
    "--proxy-url",
    "--outtmpl",
    "-o",
    "--output",
    "--cookiefile",
    "--cookies",
    "--paths",
    "-P",
    "--home-dir",
    "--exec",
    "--exec-before-download",
    "--load-info-json",
}

# Set of disallowed standalone boolean flags
DISALLOWED_STANDALONE: Set[str] = {
    "--dump-json",
    "--dump-single-json",
    "--print-json",
    "--help",
    "-h",
    "--version",
}


def sanitize_yt_dlp_args(args: List[str]) -> Tuple[List[str], List[str]]:
    """
    Strips client-provided CLI arguments that conflict with worker-defined settings,
    including proxy arguments, output templates, cookie paths, and execution hooks.

    Returns:
      (sanitized_args, stripped_args)
    """
    if not args:
        return [], []

    sanitized: List[str] = []
    stripped: List[str] = []
    skip_next = False

    for i, arg in enumerate(args):
        if skip_next:
            skip_next = False
            continue

        raw_arg = arg.strip()
        if not raw_arg:
            continue

        # Check '=' syntax e.g. --proxy=http://... or -o=path
        if "=" in raw_arg:
            prefix, _ = raw_arg.split("=", 1)
            prefix_lower = prefix.lower()
            if prefix_lower in DISALLOWED_WITH_VALUE or prefix_lower in DISALLOWED_STANDALONE:
                stripped.append(raw_arg)
                continue

        arg_lower = raw_arg.lower()

        # Check flag with separate value e.g. --proxy http://...
        if arg_lower in DISALLOWED_WITH_VALUE:
            stripped.append(raw_arg)
            if i + 1 < len(args) and not args[i + 1].startswith("-"):
                stripped.append(args[i + 1])
                skip_next = True
            continue

        # Check standalone disallowed flags e.g. --dump-json
        if arg_lower in DISALLOWED_STANDALONE:
            stripped.append(raw_arg)
            continue

        sanitized.append(raw_arg)

    return sanitized, stripped
