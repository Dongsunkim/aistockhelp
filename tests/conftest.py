"""Keep API regression cases offline; macro-specific tests override this stub."""
import pytest


@pytest.fixture(autouse=True)
def isolated_stock_identity(monkeypatch):
    """Provider/API regression fixtures may use synthetic tickers and labels.

    Identity-specific tests restore the real resolver explicitly. All other
    cases keep their original provider fixture without making network lookups.
    """
    import app
    monkeypatch.setattr(app,'resolve_stock',lambda symbol,market,name='': {
        'symbol':symbol,'market':market,'name':name,
    })


@pytest.fixture(autouse=True)
def offline_macro_provider(monkeypatch):
    import app
    monkeypatch.setattr(app, 'fetch_macro_context', lambda: {
        'eligible': False, 'summary': '오프라인 테스트에서는 미국 경제 자료를 연결하지 않았습니다.',
        'metrics': {}, 'risk_flags': [], 'warnings': ['미국 경제 자료 미연결'],
        'checked_at': '2026-11-06T00:00:00+00:00',
    })
