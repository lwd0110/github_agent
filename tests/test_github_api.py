from github_agent.github_api import RepositoryRef, parse_repository_url


def test_parse_https_url():
    assert parse_repository_url("https://github.com/huggingface/smolagents") == RepositoryRef("huggingface", "smolagents")


def test_parse_shorthand():
    assert parse_repository_url("huggingface/smolagents") == RepositoryRef("huggingface", "smolagents")

