import pytest
from backend.app.tools.run_load_test import run_load_test

def test_run_load_test_guardrail(mocker):
    """유저 수를 100명으로 과도하게 넣었을 때 가드레일(50명 제한)이 작동하는지 검증합니다."""
    # 실제로 쏘지 않고 subprocess.run을 모킹하여 파일 검증 단계만 타도록 세팅
    mock_run = mocker.patch("subprocess.run")
    mocker.patch("os.path.exists", return_value=True)
    
    # 더미 데이터용 CSV 읽기 로직 모킹
    import pandas as pd
    mock_df = pd.DataFrame([{
        'Name': 'Aggregated', 'Request Count': 100, 'Failure Count': 0,
        'Requests/s': 10.0, 'Average Response Time': 50.0, 'Min Response Time': 10.0,
        'Max Response Time': 200.0, '50%': 45.0, '95%': 120.0, '99%': 180.0
    }])
    mocker.patch("pandas.read_csv", return_value=mock_df)
    mocker.patch("os.remove")

    # 100명으로 호출 시도
    result = run_load_test("http://dummy-target", users=100, spawn_rate=10, run_time="5s")
    
    # subprocess.run 명령어 인자 목록 가져오기
    called_args = mock_run.call_args[0][0]
    
    # `--users` 옵션 뒤의 값이 50으로 제한되었는지 확인
    users_idx = called_args.index("--users")
    assert called_args[users_idx + 1] == "50"
    assert result["status"] == "success"

def test_run_load_test_invalid_aggregated(mocker):
    """CSV 결과 파일은 생성되었으나 내용에 'Aggregated' 행이 없을 때 에러 처리가 잘 되는지 확인합니다."""
    mocker.patch("subprocess.run")
    mocker.patch("os.path.exists", return_value=True)
    
    import pandas as pd
    mock_empty_df = pd.DataFrame([{'Name': 'SomeApi', 'Request Count': 10}])
    mocker.patch("pandas.read_csv", return_value=mock_empty_df)
    mocker.patch("os.remove")

    result = run_load_test("http://dummy-target", users=10, spawn_rate=2, run_time="5s")
    assert result["status"] == "error"
    assert "Aggregated" in result["message"]