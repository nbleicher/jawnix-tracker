from typer.testing import CliRunner

from jawnix_data.cli import app


def test_required_request_id_options_match_operator_contract():
    runner = CliRunner()
    redistribute = runner.invoke(app, ["redistribute", "--help"])
    retry = runner.invoke(app, ["retry-delivery", "--help"])
    assert redistribute.exit_code == 0 and "--request-id" in redistribute.stdout
    assert retry.exit_code == 0 and "--request-id" in retry.stdout
