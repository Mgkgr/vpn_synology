import { Area, AreaChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts'

import type { Period } from '../api/types'
import { formatBytes } from './Status'

export function TrafficChart({ up, down, period }: { up: number; down: number; period: Period }) {
  const points = period === 'month'
    ? ['01', '07', '14', '21', 'сегодня']
    : ['янв', 'мар', 'май', 'июл', 'сегодня']
  const data = points.map((label, index) => ({
    label,
    входящий: Math.round(down * ((index + 1) / points.length)),
    исходящий: Math.round(up * ((index + 1) / points.length)),
  }))

  return (
    <div className="traffic-chart" aria-label={`Трафик за ${period === 'month' ? 'месяц' : 'год'}`}>
      <ResponsiveContainer width="100%" height={190}>
        <AreaChart data={data} margin={{ top: 8, right: 4, left: 0, bottom: 0 }}>
          <defs>
            <linearGradient id="trafficIn" x1="0" x2="0" y1="0" y2="1"><stop stopColor="#337df6" stopOpacity=".36" /><stop offset="1" stopColor="#337df6" stopOpacity="0" /></linearGradient>
          </defs>
          <XAxis dataKey="label" axisLine={false} tickLine={false} minTickGap={24} />
          <YAxis hide />
          <Tooltip formatter={(value) => formatBytes(Number(value))} contentStyle={{ background: '#101b2c', border: '1px solid #263650', borderRadius: 0 }} />
          <Area type="monotone" dataKey="входящий" stroke="#5b9cff" fill="url(#trafficIn)" strokeWidth={2} />
          <Area type="monotone" dataKey="исходящий" stroke="#8caeff" fill="transparent" strokeWidth={1.5} strokeDasharray="4 4" />
        </AreaChart>
      </ResponsiveContainer>
    </div>
  )
}
