from decimal import Decimal


def test_deposit_validation(client, auth):
    for bad in ["0", "-5", "0.001", "100000.01"]:
        assert client.post("/wallet/deposit", json={"amount": bad}, headers=auth).status_code == 422, bad


def test_deposit_and_ledger(client, auth):
    client.post("/wallet/deposit", json={"amount": "100.50"}, headers=auth)
    client.post("/wallet/deposit", json={"amount": "20"}, headers=auth)

    assert Decimal(client.get("/wallet", headers=auth).json()["balance"]) == Decimal("120.50")
    txs = client.get("/wallet/transactions", headers=auth).json()
    assert [Decimal(t["balance_after"]) for t in txs] == [Decimal("120.50"), Decimal("100.50")]
    assert len(client.get("/wallet/transactions?limit=1", headers=auth).json()) == 1
