"""Offline regressions for explicit bounded HTTP policy and repo-file paths."""
import logging

import httpx
import pytest

import bounded_http


@pytest.mark.asyncio
async def test_explicit_follow_redirects_true_is_refused_before_request():
    requests = []
    def handler(request):
        requests.append(request)
        return httpx.Response(302, headers={"location": "https://other.invalid/"})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ValueError, match="follow_redirects"):
            await bounded_http.bounded_request(client, "GET", "https://provider.invalid/", follow_redirects=True)
    assert requests == []


@pytest.mark.asyncio
@pytest.mark.parametrize("header", ["Accept-Encoding", "accept-encoding", "ACCEPT-ENCODING"])
async def test_explicit_compression_policy_is_refused_before_request(header):
    requests = []
    def handler(request):
        requests.append(request)
        return httpx.Response(200, content=b"dummy")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ValueError, match="Accept-Encoding"):
            await bounded_http.bounded_request(client, "GET", "https://provider.invalid/", headers={header: "gzip"})
    assert requests == []


@pytest.mark.asyncio
async def test_identity_encoding_and_explicit_no_redirect_preserve_success():
    requests = []
    def handler(request):
        requests.append(request)
        return httpx.Response(200, json={"ok": True})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=True) as client:
        response = await bounded_http.bounded_request(client, "GET", "https://provider.invalid/", headers={"accept-encoding": "identity"}, follow_redirects=False)
    assert response.json() == {"ok": True}
    assert requests[0].headers.get_list("accept-encoding") == ["identity"]


@pytest.mark.asyncio
async def test_redirect_warning_is_host_only_and_once_per_host_per_hour(monkeypatch, caplog):
    now = [100.0]
    monkeypatch.setattr(bounded_http, "_redirect_warning_times", {})
    monkeypatch.setattr(bounded_http.time, "monotonic", lambda: now[0])
    requests = []
    def handler(request):
        requests.append(request)
        return httpx.Response(307, headers={"location": "https://other.invalid/private-target"})
    caplog.set_level(logging.WARNING, logger="bounded_http")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=True) as client:
        for url in ("https://dummy-user:dummy-password@provider.invalid/private?dummy-secret=value",
                    "https://provider.invalid/other", "https://second.invalid/path"):
            assert (await bounded_http.bounded_request(client, "GET", url)).status_code == 307
        now[0] = 3699.0
        await bounded_http.bounded_request(client, "GET", "https://provider.invalid/")
        assert [r.getMessage() for r in caplog.records] == [
            "redirect not followed host=provider.invalid status=307",
            "redirect not followed host=second.invalid status=307",
        ]
        now[0] = 3700.0
        await bounded_http.bounded_request(client, "GET", "https://provider.invalid/")
    assert len(requests) == 5
    assert [r.getMessage() for r in caplog.records][-1] == "redirect not followed host=provider.invalid status=307"
    assert len(caplog.records) == 3
    assert "dummy-" not in caplog.text and "private" not in caplog.text


@pytest.mark.asyncio
async def test_redirect_warning_memory_is_bounded_without_rewarning_recent_host(monkeypatch, caplog):
    monkeypatch.setattr(bounded_http, "_redirect_warning_times", {})
    monkeypatch.setattr(bounded_http, "_REDIRECT_WARNING_HOST_LIMIT", 2)
    monkeypatch.setattr(bounded_http.time, "monotonic", lambda: 100.0)
    caplog.set_level(logging.WARNING, logger="bounded_http")
    def handler(request):
        return httpx.Response(301)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        for host in ("one.invalid", "two.invalid", "three.invalid", "one.invalid"):
            await bounded_http.bounded_request(client, "GET", f"https://{host}/")
    assert len(bounded_http._redirect_warning_times) == 2
    assert len(caplog.records) == 2


def test_dockerignore_regression_is_independent_of_working_directory(tmp_path, monkeypatch):
    from tests.test_remediation_hardening import test_nested_context_exclusions_with_synthetic_markers
    monkeypatch.chdir(tmp_path)
    test_nested_context_exclusions_with_synthetic_markers(tmp_path)


@pytest.mark.parametrize("source", ["clean", "marker", "missing"])
def test_captcha_source_scan_executes_from_backend_cwd(tmp_path, monkeypatch, source):
    from tests import test_audit_77_a as audit
    backend = tmp_path / "source" / "backend"
    (backend / "tests").mkdir(parents=True)
    marker = "test" + "-captcha-pass"
    (backend / "provider.py").write_text(marker if source == "marker" else "# clean source\n")
    target = backend if source != "missing" else tmp_path / "missing" / "backend"
    monkeypatch.setattr(audit, "__file__", str(target / "tests" / "test_audit_77_a.py"))
    monkeypatch.chdir(backend)
    real_run = audit.subprocess.run
    results = []
    def record_real_scan(*args, **kwargs):
        result = real_run(*args, **kwargs)
        results.append(result)
        return result
    monkeypatch.setattr(audit.subprocess, "run", record_real_scan)
    if source == "clean":
        audit.test_repo_has_no_test_captcha_pass_in_backend_code()
    else:
        with pytest.raises(AssertionError):
            audit.test_repo_has_no_test_captcha_pass_in_backend_code()
    assert len(results) == 1
    assert results[0].returncode == {"clean": 1, "marker": 0, "missing": 2}[source]
    if source == "marker":
        assert marker in results[0].stdout
