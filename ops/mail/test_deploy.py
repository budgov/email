import unittest
from deploy import config, check_both_states, release_config, TARGETS


class ProductionConfigTests(unittest.TestCase):
    def live(self, target):
        spec = TARGETS[target]
        return {'bindings': [
            {'name': 'jwt_secret', 'type': 'secret_text'},
            {'name': 'db', 'type': 'd1', 'id': spec['database_id']},
            {'name': 'kv', 'type': 'kv_namespace', 'namespace_id': spec['kv_id']},
        ]}

    def build(self, target, live=None):
        return config(target, live or self.live(target), 'a' * 40, 'b' * 40)

    def test_each_worker_isolated(self):
        for target in TARGETS:
            cfg = self.build(target)
            self.assertEqual(cfg['name'], target)
            self.assertEqual(cfg['d1_databases'][0]['database_id'], TARGETS[target]['database_id'])
            self.assertEqual(cfg['kv_namespaces'][0]['id'], TARGETS[target]['kv_id'])
            self.assertNotIn('jwt_secret', cfg['vars'])

    def test_missing_bindings_restored_not_provisioned(self):
        cfg = self.build('juuer-mail', {'bindings': [{'name': 'jwt_secret', 'type': 'secret_text'}]})
        self.assertEqual(cfg['d1_databases'][0]['database_id'], TARGETS['juuer-mail']['database_id'])

    def test_crossed_database_and_kv_rejected(self):
        for index, key, value in [(1, 'id', TARGETS['budgov-mail']['database_id']),
                                  (2, 'namespace_id', TARGETS['budgov-mail']['kv_id'])]:
            live = self.live('juuer-mail')
            live['bindings'][index][key] = value
            with self.assertRaises(RuntimeError):
                self.build('juuer-mail', live)

    def test_optional_resources_and_mail_restrictions_preserved(self):
        live = self.live('juuer-mail')
        live['bindings'] += [
            {'name': 'r2', 'type': 'r2_bucket', 'bucket_name': 'existing'},
            {'name': 'forward', 'type': 'send_email', 'destination_address': 'owner@example.com'},
            {'name': 'email', 'type': 'send_email'},
        ]
        cfg = self.build('juuer-mail', live)
        self.assertEqual(cfg['r2_buckets'][0]['bucket_name'], 'existing')
        self.assertIn({'name': 'forward', 'destination_address': 'owner@example.com'}, cfg['send_email'])
        self.assertIn({'name': 'email'}, cfg['send_email'])

    def test_unknown_binding_stops_instead_of_deleting(self):
        live = self.live('juuer-mail')
        live['bindings'].append({'name': 'another_db', 'type': 'd1', 'id': 'other'})
        with self.assertRaises(RuntimeError):
            self.build('juuer-mail', live)

    def test_unknown_target_and_unpinned_revision_rejected(self):
        with self.assertRaises(RuntimeError):
            config('cloud-mail', {}, 'a' * 40, 'b' * 40)
        with self.assertRaises(RuntimeError):
            config('juuer-mail', self.live('juuer-mail'), 'main', 'b' * 40)

    def test_active_resource_conflict_cannot_hide_behind_draft(self):
        active = self.live('budgov-mail')
        with self.assertRaises(RuntimeError):
            check_both_states('juuer-mail', self.live('juuer-mail'), active, 'a' * 40, 'b' * 40)

    def test_incomplete_preview_is_repairable_but_cannot_pass_verification(self):
        draft = {'bindings': [{'name': 'jwt_secret', 'type': 'secret_text'}]}
        check_both_states('juuer-mail', draft, self.live('juuer-mail'), 'a' * 40, 'b' * 40)
        with self.assertRaises(RuntimeError):
            check_both_states('juuer-mail', draft, self.live('juuer-mail'), 'a' * 40, 'b' * 40, require_complete=True)

    def test_bindingless_preview_draft_does_not_block_or_drop_production_bindings(self):
        # Realistic production: every runtime binding plus optional R2 and a restricted sender.
        active = self.live('juuer-mail')
        active['bindings'] += [
            {'name': 'ai', 'type': 'ai'},
            {'name': 'assets', 'type': 'assets'},
            {'name': 'ws', 'type': 'durable_object_namespace', 'class_name': 'MailHub'},
            {'name': 'forward', 'type': 'send_email', 'allowed_destination_addresses': ['a@juuer.com']},
            {'name': 'r2', 'type': 'r2_bucket', 'bucket_name': 'existing'},
        ]
        draft = {'bindings': [{'name': 'jwt_secret', 'type': 'secret_text'}]}
        _, cfg = release_config('juuer-mail', draft, active, 'a' * 40, 'b' * 40)
        self.assertEqual(cfg['r2_buckets'], [{'binding': 'r2', 'bucket_name': 'existing'}])
        self.assertEqual(cfg['send_email'], [{'name': 'forward', 'allowed_destination_addresses': ['a@juuer.com']}])
        self.assertEqual(cfg['d1_databases'][0]['database_id'], TARGETS['juuer-mail']['database_id'])


if __name__ == '__main__':
    unittest.main()
