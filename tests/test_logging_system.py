import logging
import tempfile
import unittest
from pathlib import Path

from logging_system import configure_logging, event, exception


class LoggingSystemTests(unittest.TestCase):
    def tearDown(self):
        root = logging.getLogger()
        for handler in list(root.handlers):
            root.removeHandler(handler)
            handler.close()
        for name in ('bot', 'trading', 'risk', 'broker', 'error'):
            logger = logging.getLogger(name)
            for handler in list(logger.handlers):
                logger.removeHandler(handler)
                handler.close()

    def test_files_rotation_events_and_redaction(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
            configure_logging({'logging': {'directory': directory, 'max_bytes': 120, 'backup_count': 2, 'level': 'INFO'}})
            event('SIGNAL_REJECTED', 'quality below threshold', logger_name='trading', symbol='EURUSDm', reason='quality_below_threshold')
            event('RISK_BLOCK', 'password=never-write', logger_name='risk', reason='daily_loss_limit')
            for _ in range(20):
                event('ORDER_FAILED', 'x' * 100, logger_name='broker', level=logging.ERROR)
            try:
                raise RuntimeError('expected-test-error')
            except RuntimeError as error:
                exception('UNEXPECTED_EXCEPTION', error)
            files = {path.name for path in Path(directory).iterdir()}
            self.assertTrue({'bot.log', 'trading.log', 'risk.log', 'broker.log', 'error.log'} <= files)
            content = '\n'.join(path.read_text(encoding='utf-8') for path in Path(directory).glob('*.log*'))
            self.assertIn('SIGNAL_REJECTED', content)
            self.assertIn('[REDACTED]', content)
            self.assertIn('Traceback', content)

    def test_event_levels(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
            configure_logging({'logging': {'directory': directory, 'level': 'INFO'}})
            event('BOT_STARTED', 'safe', logger_name='bot')
            event('EMERGENCY_STOP', 'risk limit', logger_name='risk', level=logging.CRITICAL)
            self.assertIn('BOT_STARTED', Path(directory, 'bot.log').read_text(encoding='utf-8'))
            self.assertIn('EMERGENCY_STOP', Path(directory, 'risk.log').read_text(encoding='utf-8'))


if __name__ == '__main__':
    unittest.main()
