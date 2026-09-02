"""Professional multi-file logging with rotation, redaction and structured events."""
import json
import logging
import re
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Callable, Dict, Optional

SECRET_PATTERN = re.compile(r"(?i)(password|token|secret|api[_-]?key|authorization|access[_-]?token)")

class SecretRedactionFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if not hasattr(record, 'event'): record.event = 'LOG'
        record.msg = self._redact(record.msg)
        if record.args:
            record.args = tuple(self._redact(value) for value in record.args)
        return True
    @staticmethod
    def _redact(value: Any) -> Any:
        if not isinstance(value, str): return value
        return SECRET_PATTERN.sub(lambda match: match.group(1) + "=[REDACTED]", value)

class CriticalAlertHandler(logging.Handler):
    def __init__(self, callback: Optional[Callable[[str], None]] = None):
        super().__init__(level=logging.CRITICAL); self.callback=callback
    def emit(self, record):
        if self.callback:
            try: self.callback(self.format(record))
            except Exception: self.handleError(record)

def configure_logging(config: Optional[dict] = None, alert_callback: Optional[Callable[[str], None]] = None) -> Dict[str, logging.Logger]:
    settings=(config or {}).get('logging', {}); directory=Path(settings.get('directory','logs')); directory.mkdir(parents=True, exist_ok=True)
    max_bytes=int(settings.get('max_bytes',10*1024*1024)); backups=int(settings.get('backup_count',5)); level=getattr(logging,str(settings.get('level','INFO')).upper(),logging.INFO)
    root=logging.getLogger(); root.setLevel(level)
    for handler in list(root.handlers): root.removeHandler(handler); handler.close()
    for logger_name in ('bot', 'trading', 'risk', 'broker', 'error'):
        named_logger = logging.getLogger(logger_name)
        for handler in list(named_logger.handlers): named_logger.removeHandler(handler); handler.close()
    formatter=logging.Formatter('%(asctime)s %(levelname)s %(name)s event=%(event)s %(message)s')
    redactor=SecretRedactionFilter(); loggers={}
    paths={'bot':'bot.log','trading':'trading.log','risk':'risk.log','broker':'broker.log','error':'error.log'}
    for name,filename in paths.items():
        logger=logging.getLogger(name); logger.setLevel(level); logger.propagate=False
        handler=RotatingFileHandler(directory/filename,maxBytes=max_bytes,backupCount=backups,encoding='utf-8'); handler.setFormatter(formatter); handler.addFilter(redactor)
        if name=='error': handler.setLevel(logging.ERROR)
        logger.addHandler(handler); loggers[name]=logger
    root_bot = RotatingFileHandler(directory / 'bot.log', maxBytes=max_bytes, backupCount=backups, encoding='utf-8')
    root_bot.setFormatter(formatter); root_bot.addFilter(redactor); root.addHandler(root_bot)
    console=logging.StreamHandler(); console.setFormatter(formatter); console.addFilter(redactor); root.addHandler(console)
    alert=CriticalAlertHandler(alert_callback); alert.setFormatter(formatter); alert.addFilter(redactor); root.addHandler(alert)
    return loggers

def event(name: str, message: str = '', logger_name: str = 'bot', level: int = logging.INFO, **fields: Any) -> None:
    logger=logging.getLogger(logger_name); payload={key: value for key,value in fields.items() if value is not None}
    suffix = (' '+json.dumps(payload,ensure_ascii=True,default=str)) if payload else ''
    logger.log(level, '%s %s%s', name, message, suffix, extra={'event':name})

def exception(name: str, error: BaseException, logger_name: str = 'error', **fields: Any) -> None:
    logging.getLogger(logger_name).exception('%s %s', name, error, extra={'event':name, **fields})
