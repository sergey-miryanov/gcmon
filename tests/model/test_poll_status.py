import pytest

from gcmon.model.poll_status import PollStatus


@pytest.fixture
def poll_status_list() -> list[PollStatus]:
    return list(PollStatus)


class TestPollStatusMembers:
    def test_all_members_present(self, poll_status_list: list[PollStatus]) -> None:
        assert poll_status_list == [
            PollStatus.OK,
            PollStatus.FAIL,
            PollStatus.INVALID_PROCESS,
        ]
