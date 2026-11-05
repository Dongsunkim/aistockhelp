"""Regressions for the legacy multibyte encoding used by Naver FCHART."""
from datetime import datetime,timedelta
import pandas as pd
import pytest
from naver_history import KST,fetch_naver_daily,xml_rows


@pytest.mark.parametrize('encoding',['euc-kr','EUC-KR','cp949','utf-8'])
def test_declared_xml_encoding_with_korean_text(encoding):
    xml=('<?xml version="1.0" encoding="'+encoding+'"?>'
         '<protocol><chartdata name="삼성전자">'
         '<item data="20261102|100|120|90|110|1000"/>'
         '</chartdata></protocol>').encode(encoding)
    assert xml_rows(xml)==[{'date':'2026-11-02','open':100.,'high':120.,
                           'low':90.,'close':110.,'volume':1000.}]


def test_unicode_xml_already_decoded():
    assert xml_rows('<?xml version="1.0" encoding="EUC-KR"?>'
                    '<protocol><item data="20261102|100|120|90|110|1000"/></protocol>')[0]['close']==110


@pytest.mark.parametrize('xml',[
    b'<?xml version="1.0" encoding="unknown-codec"?><protocol/>',
    b'<?xml version="1.0" encoding="utf-8"?><protocol>\xff</protocol>',
])
def test_invalid_encoded_xml_fails_closed(xml):
    with pytest.raises(ValueError,match='인코딩'):
        xml_rows(xml)


def test_euckr_fchart_full_fetch_does_not_need_mobile_fallback():
    end=datetime.now(KST).date()-timedelta(days=1)
    dates=pd.bdate_range(end=end,periods=200)
    items=''.join('<item data="'+d.strftime('%Y%m%d')+'|100|120|90|110|1000"/>' for d in dates)
    xml=('<?xml version="1.0" encoding="euc-kr"?>'
         '<protocol><chartdata name="삼성전자">'+items+'</chartdata></protocol>').encode('euc-kr')
    class Response:
        content=xml
        def raise_for_status(self):pass
    class Session:
        def get(self,url,**kwargs):
            assert url=='https://fchart.stock.naver.com/sise.nhn'
            assert kwargs['params']['symbol']=='005930'
            return Response()
    bars,metadata=fetch_naver_daily('005930','KOSPI',session=Session())
    assert len(bars)==200
    assert metadata['endpoint']=='NAVER_FCHART'
    assert metadata['source']=='NAVER'
