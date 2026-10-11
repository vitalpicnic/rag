from pathlib import Path
import tempfile
import unittest

ROOT=Path(__file__).resolve().parents[1]


class DeploymentTests(unittest.TestCase):
    def test_local_smoke_uses_real_faiss_and_synthetic_generation(self):
        from scripts.smoke_local import smoke
        from rag.embeddings import local_profile
        class Embedding:
            profile=local_profile('a'*40,{})
            def embed_documents(self,texts): return [[1.0]+[0.0]*383 for _ in texts]
            def embed_query(self,text): return [1.0]+[0.0]*383
        report=smoke(Embedding())
        self.assertEqual(report['status'],'passed')
        self.assertFalse(report['real_generation'])
        self.assertTrue(report['faiss_unchanged'])

    def test_existing_weak_credentials_are_rejected(self):
        import json
        from scripts.init_secrets import initialize
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            for name in ('cli','web','tg','health'): (root/(name+'.txt')).write_text('weak')
            (root/'adapters.json').write_text(json.dumps(dict.fromkeys(('cli','web','tg','health'),'weak')))
            with self.assertRaises(ValueError): initialize(root)

    def test_compose_has_real_limits_and_private_ports(self):
        import yaml
        config=yaml.safe_load((ROOT/'compose.yaml').read_text())
        services=config['services']
        self.assertEqual(services['runtime']['mem_limit'],'3584m')
        self.assertEqual(services['indexer']['mem_limit'],'4608m')
        for service in services.values():
            self.assertTrue(service['read_only'])
            self.assertEqual(service['user'],'10001:10001')
            self.assertIn('ALL',service['cap_drop'])
            self.assertIn('no-new-privileges:true',service['security_opt'])
            self.assertNotIn('docker.sock',str(service))
            for port in service.get('ports',[]): self.assertTrue(port.startswith('127.0.0.1:'))

    def test_image_excludes_private_data_and_uses_hashes(self):
        docker=(ROOT/'Dockerfile').read_text()
        self.assertIn('@sha256:',docker)
        self.assertIn('--require-hashes',docker)
        ignore=(ROOT/'.dockerignore').read_text()
        for name in ('.git','data','model_cache','deploy/secrets','.env'): self.assertIn(name,ignore)
        lock=(ROOT/'deploy/requirements-linux.lock').read_text()
        self.assertIn('torch==2.14.1+cpu',lock)
        self.assertIn('--hash=sha256:',lock)

    def test_secrets_setup_is_idempotent_and_no_generation_mode_is_honest(self):
        from scripts.init_secrets import initialize
        from scripts.serve_runtime import NoGenerationRuntime
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            initialize(root/'secrets')
            original=(root/'secrets/adapters.json').read_bytes()
            initialize(root/'secrets')
            self.assertEqual(original,(root/'secrets/adapters.json').read_bytes())
            runtime=NoGenerationRuntime(root)
            try:
                self.assertFalse(runtime.readiness()['ready'])
                with self.assertRaises(RuntimeError): runtime.query({})
            finally: runtime.drain()
