import { Area, AreaChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts'

import type { RealtimeTrafficPoint } from '../api/types'
import { formatDate, formatRate } from './Status'

function formatTick(value: string): string {
  const date = new Date(value)
  return Number.isNaN(date.valueOf())
    ? '—'
    : new Intl.DateTimeFormat('ru-RU', { hour: '2-digit', minute: '2-digit' }).format(date)
}

export function TrafficChart({ points }: { points: RealtimeTrafficPoint[] }) {
  if (!points.length) {
    return <p className="state-message traffic-chart">Первая точка появится после минутного сбора данных.</p>
  }

  return (
    <div className="traffic-chart" aria-label="Скорость канала за выбранный период">
      <ResponsiveContainer width="100%" height={190}>
        <AreaChart data={points} margin={{ top: 8, right: 4, left: 0, bottom: 0 }}>
          <defs>
            <linearGradient id="trafficIn" x1="0" x2="0" y1="0" y2="1"><stop stopColor="#337df6" stopOpacity=".36" /><stop offset="1" stopColor="#337df6" stopOpacity="0" /></linearGradient>
          </defs>
          <XAxis dataKey="observed_at" tickFormatter={formatTick} axisLine={false} tickLine={false} minTickGap={30} />
          <YAxis hide />
          <Tooltip
            labelFormatter={(value) => formatDate(String(value))}
            formatter={(value, name) => [formatRate(Number(value)), name === 'down_bps' ? 'Входящий' : 'Исходящий']}
            contentStyle={{ background: '#101b2c', border: '1px solid #263650', borderRadius: 0 }}
          />
          <Area type="monotone" dataKey="down_bps" name="Входящий" stroke="#5b9cff" fill="url(#trafficIn)" strokeWidth={2} />
          <Area type="monotone" dataKey="up_bps" name="Исходящий" stroke="#8caeff" fill="transparent" strokeWidth={1.5} strokeDasharray="4 4" />
        </AreaChart>
      </ResponsiveContainer>
    </div>
  )
}
