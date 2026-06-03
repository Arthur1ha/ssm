const { useRef: useRefRadar, useEffect: useEffectRadar } = React;

const RADAR_R     = 100;
const RADAR_MAX_M = 500;

function RadarScan({ agents, phoneLoc }) {
  const canvasRef   = useRefRadar(null);
  const agentsRef   = useRefRadar(agents);
  const phoneLocRef = useRefRadar(phoneLoc);

  useEffectRadar(() => { agentsRef.current   = agents;   }, [agents]);
  useEffectRadar(() => { phoneLocRef.current = phoneLoc; }, [phoneLoc]);

  useEffectRadar(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;

    const SIZE = 240;
    const dpr  = window.devicePixelRatio || 1;
    canvas.width  = SIZE * dpr;
    canvas.height = SIZE * dpr;
    const ctx = canvas.getContext('2d');
    ctx.scale(dpr, dpr);

    let sweep     = 0;
    let prevSweep = 0;
    const pingAge = {};

    function hexAlpha(hex, a) {
      const r = parseInt(hex.slice(1, 3), 16);
      const g = parseInt(hex.slice(3, 5), 16);
      const b = parseInt(hex.slice(5, 7), 16);
      return `rgba(${r},${g},${b},${a})`;
    }

    function agentRadarPos(agent, phone) {
      if (!phone || agent._lat == null) return null;
      const dist = haversine(phone.lat, phone.lng, agent._lat, agent._lng);
      const brng  = bearing(phone.lat, phone.lng, agent._lat, agent._lng);
      const r     = Math.min(dist / RADAR_MAX_M, 1) * RADAR_R;
      const rad   = brng * Math.PI / 180;
      return { x: 120 + Math.sin(rad) * r, y: 120 - Math.cos(rad) * r, bearing: brng };
    }

    function draw() {
      const agts  = agentsRef.current;
      const phone = phoneLocRef.current;

      ctx.clearRect(0, 0, SIZE, SIZE);

      const RINGS = [
        { r: RADAR_R * 100 / RADAR_MAX_M, label: '100m' },
        { r: RADAR_R * 250 / RADAR_MAX_M, label: '250m' },
        { r: RADAR_R,                      label: `${RADAR_MAX_M}m` },
      ].filter(rr => rr.r <= RADAR_R);
      RINGS.forEach(({ r, label }) => {
        ctx.strokeStyle = 'rgba(200,255,62,0.1)';
        ctx.lineWidth = 1;
        ctx.beginPath(); ctx.arc(120, 120, r, 0, Math.PI * 2); ctx.stroke();
        ctx.fillStyle = 'rgba(200,255,62,0.3)';
        ctx.font = '8px monospace'; ctx.textAlign = 'center';
        ctx.fillText(label, 120, 120 - r - 2);
      });

      ctx.strokeStyle = 'rgba(200,255,62,0.06)';
      ctx.lineWidth = 1;
      ctx.setLineDash([4, 4]);
      ctx.beginPath(); ctx.moveTo(0, 120); ctx.lineTo(SIZE, 120); ctx.stroke();
      ctx.beginPath(); ctx.moveTo(120, 0); ctx.lineTo(120, SIZE); ctx.stroke();
      ctx.setLineDash([]);

      ctx.fillStyle = 'rgba(200,255,62,0.4)';
      ctx.font = 'bold 9px monospace'; ctx.textAlign = 'center';
      ctx.fillText('N', 120, 14);

      const sweepRad = sweep * Math.PI / 180;
      ctx.save();
      ctx.translate(120, 120);
      ctx.rotate(sweepRad);
      ctx.beginPath();
      ctx.moveTo(0, 0);
      ctx.arc(0, 0, RADAR_R, -6 * Math.PI / 180, 24 * Math.PI / 180);
      ctx.closePath();
      ctx.fillStyle = 'rgba(200,255,62,0.08)';
      ctx.fill();
      const armGrad = ctx.createLinearGradient(0, 0, RADAR_R, 0);
      armGrad.addColorStop(0, 'rgba(200,255,62,0.9)');
      armGrad.addColorStop(1, 'rgba(200,255,62,0)');
      ctx.strokeStyle = armGrad;
      ctx.lineWidth = 2;
      ctx.shadowColor = 'rgba(200,255,62,0.5)';
      ctx.shadowBlur = 8;
      ctx.beginPath(); ctx.moveTo(0, 0); ctx.lineTo(RADAR_R, 0); ctx.stroke();
      ctx.shadowBlur = 0;
      ctx.restore();

      prevSweep = sweep;
      sweep     = (sweep + 1.2) % 360;
      agts.forEach(a => {
        const pos = agentRadarPos(a, phone);
        if (!pos) return;
        const uid  = a.unit_id || a.agent_id;
        const d    = angleDiff(sweep,     pos.bearing);
        const prev = angleDiff(prevSweep, pos.bearing);
        if (d < 4 && prev >= 4) pingAge[uid] = 0;
      });
      Object.keys(pingAge).forEach(k => { pingAge[k]++; });

      agts.forEach(a => {
        const uid  = a.unit_id || a.agent_id;
        const meta = getAgentMeta(a);
        const pos  = agentRadarPos(a, phone);
        if (!pos) return;
        const { x, y } = pos;
        const age  = pingAge[uid] ?? 999;
        const ping = age < 45;
        const glow = ping ? Math.max(0, 1 - age / 45) : 0;
        const col  = a._online ? meta.color : 'rgba(255,255,255,0.25)';
        const isHex = col.startsWith('#');
        if (ping && glow > 0.05) {
          ctx.beginPath();
          ctx.arc(x, y, 10 + glow * 12, 0, Math.PI * 2);
          ctx.strokeStyle = isHex ? hexAlpha(col, glow * 0.7) : col;
          ctx.lineWidth = 1;
          ctx.stroke();
        }
        ctx.beginPath(); ctx.arc(x, y, 10, 0, Math.PI * 2);
        ctx.fillStyle = a._online && isHex ? hexAlpha(col, 0.16) : 'rgba(255,255,255,0.06)';
        ctx.fill();
        ctx.strokeStyle = col;
        ctx.lineWidth = 1.5;
        ctx.shadowColor = col;
        ctx.shadowBlur = ping ? 8 + glow * 10 : 4;
        ctx.stroke();
        ctx.shadowBlur = 0;
        ctx.beginPath(); ctx.arc(x, y, 2.5, 0, Math.PI * 2);
        ctx.fillStyle = col; ctx.fill();
      });

      ctx.beginPath(); ctx.arc(120, 120, 6, 0, Math.PI * 2);
      ctx.fillStyle = '#fff';
      ctx.shadowColor = 'rgba(255,255,255,0.5)';
      ctx.shadowBlur = 12;
      ctx.fill();
      ctx.shadowBlur = 0;
      ctx.strokeStyle = '#0B0B0E';
      ctx.lineWidth = 2;
      ctx.stroke();

      rafId = requestAnimationFrame(draw);
    }

    let rafId = requestAnimationFrame(draw);
    return () => cancelAnimationFrame(rafId);
  }, []);

  return (
    <canvas ref={canvasRef} style={{ display: 'block', width: 240, height: 240, margin: '0 auto' }}/>
  );
}

window.RadarScan = RadarScan;
