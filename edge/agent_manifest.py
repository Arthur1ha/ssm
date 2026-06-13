# agent_manifest.py — Publish retained manifests for all present units.
# Fully driven by UNIT_CONFIGS + PRESENCE; no unit-specific logic here.
import time
from config import DEVICE_ID, FIRMWARE_VER, UNIT_CONFIGS
from probe import PRESENCE


def publish(mqtt):
    ts = time.time()
    count = 0
    for unit_id, cfg in UNIT_CONFIGS.items():
        base = "ssm/agents/{}".format(unit_id)
        topic = base + "/manifest"

        if not PRESENCE.get(unit_id, False):
            # clear any previously retained manifest so PWA drops this unit
            mqtt.publish(topic, b"", retain=True)
            continue

        manifest = {
            'unit_id':      unit_id,
            'parent_id':    DEVICE_ID,
            'agent_type':   cfg['agent_type'],
            'name':         cfg['name'],
            'hw_platform':  'esp32',
            'firmware_ver': FIRMWARE_VER,
            'ts':           ts,
            'topics': {
                'manifest': topic,
                'state':    base + "/state",
                'event':    base + "/event",
                'report':   base + "/report",
            },
        }
        if cfg['agent_type'] == 'actuator':
            manifest['topics']['command'] = base + "/command"
        manifest.update(cfg.get('manifest', {}))
        mqtt.publish(topic, manifest, retain=True)
        count += 1

    print("[Manifest] Published {} manifests for {}".format(count, DEVICE_ID))
