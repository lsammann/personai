"""Hand-written fakes for the Gmail service, shared by the test modules.

Moved here from `test_gmail_client.py` in Phase 3 step 7 so `test_agent.py`
can run the real `gmail_client.fetch` and `modify_labels` against them
(step 7 decision Q9). A test where the agent passed label names instead of
ids then fails on `modify_labels`' own check, which a recorder standing in
for `modify_labels` would accept.

Hand written rather than a Mock: the chain under test is
`svc.users().messages().list(...).execute()`, and a Mock would happily answer
any chain at all, including a wrong one.
"""

from __future__ import annotations

import json

from googleapiclient.errors import HttpError


class FakeExecutable:
    def __init__(self, result):
        self._result = result

    def execute(self):
        return self._result


class FakeMessages:
    """`get_request` and `modify_request` replace the default executable,
    so a test can make either call fail (e.g. with a `FlakyRequest`)."""

    def __init__(self, pages=None, message=None, modify_request=None, get_request=None):
        self._pages = list(pages or [])
        self._message = message
        self._modify_request = modify_request
        self._get_request = get_request
        self.list_calls: list[dict] = []
        self.get_calls: list[dict] = []
        self.modify_calls: list[dict] = []

    def list(self, **kwargs):
        self.list_calls.append(kwargs)
        return FakeExecutable(self._pages.pop(0) if self._pages else {})

    def get(self, **kwargs):
        self.get_calls.append(kwargs)
        if self._get_request is not None:
            return self._get_request
        return FakeExecutable(self._message)

    def modify(self, **kwargs):
        self.modify_calls.append(kwargs)
        if self._modify_request is not None:
            return self._modify_request
        return FakeExecutable({"id": kwargs["id"], "labelIds": ["Label_2", "Label_6"]})


class FakeLabels:
    def __init__(self, existing: dict[str, str] | None = None):
        self._existing = dict(existing or {})
        self.list_calls: list[dict] = []
        self.create_calls: list[dict] = []

    def list(self, **kwargs):
        self.list_calls.append(kwargs)
        return FakeExecutable(
            {"labels": [{"name": n, "id": i} for n, i in self._existing.items()]}
        )

    def create(self, **kwargs):
        self.create_calls.append(kwargs)
        return FakeExecutable({"id": "Label_new", "name": kwargs["body"]["name"]})


class FakeService:
    def __init__(self, messages: FakeMessages | None = None, labels: FakeLabels | None = None):
        self._messages = messages
        self._labels = labels

    def users(self):
        return self

    def messages(self):
        return self._messages

    def labels(self):
        return self._labels


# --- failures ----------------------------------------------------------------


class FakeHttpResponse:
    """The two attributes HttpError reads.

    A stub rather than httplib2.Response: httplib2 is a transitive dependency
    of google-api-python-client, not one this project declares, and reaching
    through to it in tests would make an undeclared package load-bearing.
    """

    def __init__(self, status: int):
        self.status = status
        self.reason = ""


def http_error(status: int, reason: str = "") -> HttpError:
    """An HttpError shaped like the ones Gmail actually returns."""
    content = json.dumps(
        {"error": {"code": status, "message": reason, "errors": [{"reason": reason}]}}
    ).encode()
    return HttpError(FakeHttpResponse(status), content)


class FlakyRequest:
    """Fails with `error` for the first `failures` calls, then succeeds.

    `error` may be any exception, not only an HttpError, so a test can make
    a call fail the way an auth or transport failure would.
    """

    def __init__(self, error: BaseException, failures: int, result=None):
        self._error = error
        self._remaining = failures
        self._result = result or {"messages": []}
        self.attempts = 0

    def execute(self):
        self.attempts += 1
        if self._remaining:
            self._remaining -= 1
            raise self._error
        return self._result
