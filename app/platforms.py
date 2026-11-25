"""Frontend button -> which login (provider) and which agent handles it.

Vercel, Netlify and AWS Amplify deploy from git, so they use the GitHub login and the git agent.
GitLab / Bitbucket are part of the git agent too, but come after GitHub.
"""
from dataclasses import dataclass


@dataclass(frozen=True)
class Provider:
    name: str        # github
    host: str        # github.com
    git_user: str    # username used with the token over HTTPS


PROVIDERS: dict[str, Provider] = {
    "github": Provider("github", "github.com", "x-access-token"),
}

# button -> provider (only buttons that work today)
PLATFORMS: dict[str, str] = {
    "github": "github",
    "vercel": "github",
    "netlify": "github",
    "aws_amplify": "github",
}

# buttons on the frontend that are not available yet
COMING_LATER = {"gitlab", "bitbucket", "wordpress", "vps", "aws_ec2"}


def provider_for(platform: str) -> Provider | None:
    name = PLATFORMS.get(platform)
    return PROVIDERS[name] if name else None
