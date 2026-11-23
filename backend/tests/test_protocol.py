"""Contract tests use TEST-ONLY values; these are not the unreceived original 22 tests."""
import json
from decimal import Decimal
from uuid import UUID

import pytest
from pydantic import ValidationError

from legal_workbench.schemas import (ListMaterialsInput, ReadSourceInput, SearchCaseInput,
                                      SearchHit, SearchMode, SearchScope, SaveAnalysisInput)

CASE = UUID('00000000-0000-4000-8000-000000000001')
SOURCE = UUID('00000000-0000-4000-8000-000000000002')


def test_case_id_required():
    with pytest.raises(ValidationError):
        ReadSourceInput(source_ref=SOURCE)


def test_invalid_enum_and_amount_currency():
    with pytest.raises(ValidationError):
        SearchCaseInput(case_id=CASE, query='合同', mode='bogus')
    with pytest.raises(ValidationError):
        SearchCaseInput(case_id=CASE, query='100', mode=SearchMode.AMOUNT)
    with pytest.raises(ValidationError):
        SearchCaseInput(case_id=CASE, query='100', mode=SearchMode.AMOUNT, currency='cny')


@pytest.mark.parametrize('kwargs', [{'offset': -1}, {'limit': -1}, {'limit': 201}])
def test_pagination_invalid(kwargs):
    with pytest.raises(ValidationError):
        ListMaterialsInput(case_id=CASE, **kwargs)


def test_search_pagination_and_context_limits():
    with pytest.raises(ValidationError):
        SearchCaseInput(case_id=CASE, query='合同', mode='literal', limit=101)
    with pytest.raises(ValidationError):
        ReadSourceInput(case_id=CASE, source_ref=SOURCE, context_chars=2001)
    assert ReadSourceInput(case_id=CASE, source_ref=SOURCE).context_chars == 200


def test_uuid_decimal_json_and_no_float_tail():
    obj = SearchHit(source=None, source_type=SearchScope.ANALYSIS, analysis_id=CASE, analysis_version_id=SOURCE, context='100.10元',
                    match_start=0, match_end=6, amount=Decimal('100.10'), currency='CNY')
    assert json.loads(obj.model_dump_json())['amount'] == '100.10'
    assert json.loads(ReadSourceInput(case_id=CASE, source_ref=SOURCE).model_dump_json())['case_id'] == str(CASE)
    with pytest.raises(ValidationError):
        SearchHit(source=None, source_type='analysis', analysis_id=CASE, analysis_version_id=SOURCE, context='', match_start=0, match_end=0, amount=100.1)


def test_analysis_update_requires_revision():
    with pytest.raises(ValidationError):
        SaveAnalysisInput(case_id=CASE, title='虚构', content='', statement_kind='excerpt', source_refs=[], analysis_id=SOURCE)


def test_upload_schema_allows_files_with_contract_fields():
    from pathlib import Path
    spec = json.loads((Path(__file__).resolve().parents[2] / 'docs/openapi.json').read_text())
    schema = spec['paths']['/api/v1/cases/{case_id}/intakes/upload']['post']['requestBody']['content']['multipart/form-data']['schema']
    example = {'case_id': str(CASE), 'source_label': 'TEST-ONLY', 'request_key': 'test',
               'relative_paths': ['虚构/测试.txt'], 'files': ['test bytes']}
    assert schema['type'] == 'object'
    assert 'allOf' not in schema
    assert set(schema['required']) <= example.keys()
    assert schema['additionalProperties'] is False
    assert set(example) <= schema['properties'].keys()
    assert schema['properties']['files']['items']['format'] == 'binary'


def test_required_operations_and_generated_types():
    from pathlib import Path
    root = Path(__file__).resolve().parents[2]
    spec = json.loads((root / 'docs/openapi.json').read_text())
    expected = {
        ('/api/v1/cases/{case_id}/intakes/{intake_id}/items', 'get', 'list_intake_items'),
        ('/api/v1/cases/{case_id}/jobs/{job_id}/retry', 'post', 'retry_job'),
        ('/api/v1/cases/{case_id}/file-versions/{file_version_id}/preview', 'get', 'preview_file_version'),
        ('/api/v1/cases/{case_id}/file-versions/{file_version_id}/download', 'get', 'download_file_version'),
        ('/api/v1/cases/{case_id}/analyses', 'get', 'list_analyses'),
        ('/api/v1/cases/{case_id}/analyses/{analysis_id}', 'get', 'get_analysis'),
        ('/api/v1/cases/{case_id}/analyses/{analysis_id}/versions', 'get', 'list_analysis_versions'),
        ('/api/v1/cases/{case_id}/analyses/{analysis_id}/versions/{version_id}', 'get', 'get_analysis_version'),
    }
    types = (root / 'frontend/src/api/types.ts').read_text()
    for path, method, operation in expected:
        assert spec['paths'][path][method]['operationId'] == operation
        assert f'{operation}:' in types
    assert 'analysis_version_id' in types
    assert '/api/v1/session' not in spec['paths']
    assert 'security' not in spec
    write = spec['paths']['/api/v1/cases']['post']
    assert [(p['name'], p['required']) for p in write['parameters']] == [('Origin', True)]


def test_hit_offsets_and_analysis_version():
    with pytest.raises(ValidationError):
        SearchHit(source=None, source_type='analysis', analysis_id=CASE, context='虚构', match_start=0, match_end=2)
    hit = SearchHit(source=None, source_type='analysis', analysis_id=CASE, analysis_version_id=SOURCE,
                    context='甲😀乙', match_start=1, match_end=2)
    assert hit.context[hit.match_start:hit.match_end] == '😀'
    with pytest.raises(ValidationError):
        SearchHit(source=None, source_type='analysis', analysis_id=CASE, analysis_version_id=SOURCE,
                  context='甲😀乙', match_start=1, match_end=4)
