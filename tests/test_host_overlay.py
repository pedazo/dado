import json
from dado.host import environment
from dado.models import empty_profile


def test_overlay_changes_only_model_fields_and_preserves_policy():
    profile = empty_profile()
    profile['dado-worker'] = {'model': 'provider/model', 'variant': 'high'}
    shared = {'policies': [{'action': 'tool.use', 'resource': 'shell:git push *', 'effect': 'deny'}],
              'agents': {'dado-worker': {'description': 'Existing'}}}
    base = {'OPENCODE_CONFIG_CONTENT': json.dumps(shared), 'SENTINEL': 'unchanged'}
    result = environment(profile, base)
    parsed = json.loads(result['OPENCODE_CONFIG_CONTENT'])
    assert parsed['policies'] == shared['policies']
    assert parsed['agents']['dado-worker'] == {'description': 'Existing', 'model': 'provider/model#high'}
    assert result['SENTINEL'] == 'unchanged'
    assert json.loads(base['OPENCODE_CONFIG_CONTENT']) == shared


def test_inherit_retains_common_model_not_false_null_override():
    base = {'OPENCODE_CONFIG_CONTENT': '{"agents":{"dado-worker":{"model":"team/model"}}}'}
    assert json.loads(environment(empty_profile(), base)['OPENCODE_CONFIG_CONTENT'])['agents']['dado-worker']['model'] == 'team/model'
