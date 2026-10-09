import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { api } from '../api/client'
import { AntidpiStrategiesPage } from './AntidpiStrategiesPage'

const revision='a'.repeat(64), jobId='b'.repeat(32)
const service={service_id:'youtube',name:'YouTube',host:'www.youtube.com',strategy_id:'tlsrec-sni',previous_strategy_id:'split-1',
  enabled:true,mode:'auto',interval_minutes:30,daily_enabled:true,state:'suspect',failure_count:1,first_failure_at:1791370000,
  last_success_at:1791369000,last_check:{checked_at:1791370000,verdict:'transport_error',reason:'timeout',latency_ms:null,http_status:null},
  history:[],results:[]}
const snapshot={available:true,can_manage:true,revision,identity:revision,observed_at:1791370010,
  catalog:{version:'byedpi-https-v1',strategies:['tlsrec-sni','disorder-1','split-1'],intervals:[5,15,30,60],daily_time:'05:30',timezone:'Asia/Yekaterinburg'},
  capabilities:{can_check:true,can_apply:true,can_configure:true,blockers:[]},services:[service]}
function mount(path='/routes/antidpi/strategies') {
  const client=new QueryClient({defaultOptions:{queries:{retry:false},mutations:{retry:false}}})
  render(<QueryClientProvider client={client}><MemoryRouter initialEntries={[path]}><AntidpiStrategiesPage /></MemoryRouter></QueryClientProvider>)
  return client
}
beforeEach(()=>{
  vi.spyOn(api,'antidpiStrategies').mockResolvedValue(structuredClone(snapshot) as never)
  vi.spyOn(api,'maintenanceJob').mockResolvedValue({job_id:jobId,phase:'unknown',cancel_allowed:false})
  vi.spyOn(api,'maintenanceJobs').mockResolvedValue({available:true,jobs:[]})
  vi.spyOn(api,'authorizeMaintenance').mockResolvedValue({grant:'ephemeral',expires_in:300})
  vi.spyOn(api,'submitMaintenance').mockImplementation(async id=>({job_id:id,phase:'queued',cancel_allowed:true}))
})
afterEach(()=>{cleanup();vi.restoreAllMocks()})

it('identifies the configured engine without presenting ByeDPI as Zapret', async()=>{
  mount()
  const engine = await screen.findByRole('region', {name:'Движок обхода DPI'})
  expect(within(engine).getByRole('heading', {name:'ByeDPI'})).toBeVisible()
  expect(within(engine).getByText(/не Zapret/)).toBeVisible()
})

it('reveals an existing blocking job and its progress after returning to the page', async()=>{
  vi.mocked(api.maintenanceJobs).mockResolvedValue({available:true,jobs:[{job_id:jobId,phase:'preflight',cancel_allowed:true}]})
  vi.mocked(api.maintenanceJob).mockResolvedValue({job_id:jobId,phase:'preflight',cancel_allowed:true,
    strategy:{step:'checking',completed:8,limit:60,kind:'manual',results:[]}})
  mount()
  fireEvent.click(await screen.findByRole('button',{name:'Показать текущее задание'}))
  expect(await screen.findByText(/Попыток завершено: 8/)).toBeVisible()
  expect(screen.getByRole('button',{name:'Проверить YouTube сейчас'})).toBeDisabled()
  expect(screen.getByRole('button',{name:'Отменить задание'})).toBeEnabled()
  expect(api.submitMaintenance).not.toHaveBeenCalled()
})

it('explains unavailable job status instead of silently disabling all controls', async()=>{
  vi.mocked(api.maintenanceJobs).mockResolvedValue({available:false,jobs:[]})
  mount()
  expect(await screen.findByRole('alert')).toHaveTextContent(/состояние заданий/)
  expect(screen.getByRole('button',{name:'Проверить YouTube сейчас'})).toBeDisabled()
})

it('separates active strategy from draft and never probes on mount',async()=>{
  mount()
  expect(await screen.findByText('Сбой подтверждается · 1/3')).toBeVisible()
  expect(screen.getByText('Текущая: tlsrec-sni')).toBeVisible()
  fireEvent.change(screen.getByLabelText('Стратегия YouTube'),{target:{value:'disorder-1'}})
  expect(screen.getByText('Текущая: tlsrec-sni')).toBeVisible()
  expect(screen.getByText(/Выбрано, но не применено/)).toBeVisible()
  expect(api.submitMaintenance).not.toHaveBeenCalled()
})

it('manual test requires focused owner dialog and has no automatic apply settings',async()=>{
  mount();fireEvent.click(await screen.findByRole('button',{name:'Проверить YouTube сейчас'}))
  const dialog=screen.getByRole('dialog')
  expect(within(dialog).getByLabelText('Пароль владельца')).toHaveFocus()
  fireEvent.change(screen.getByLabelText('Пароль владельца'),{target:{value:'local-test-password'}})
  fireEvent.click(within(dialog).getByRole('button',{name:'Подтвердить'}))
  await waitFor(()=>expect(api.submitMaintenance).toHaveBeenCalledTimes(1))
  expect(vi.mocked(api.submitMaintenance).mock.calls[0][1]).toEqual({action:'strategy_check',service_id:'youtube',expected_revision:revision,settings:{}})
  expect(JSON.stringify(vi.mocked(api.submitMaintenance).mock.calls)).not.toContain('local-test-password')
})

it('saves selected interval and daily setting only through explicit configure',async()=>{
  mount();await screen.findByText('Текущая: tlsrec-sni')
  fireEvent.change(screen.getByLabelText('Интервал YouTube'),{target:{value:'15'}})
  fireEvent.click(screen.getByLabelText('Суточный подбор YouTube'))
  fireEvent.click(screen.getByRole('button',{name:'Сохранить расписание YouTube'}))
  fireEvent.change(screen.getByLabelText('Пароль владельца'),{target:{value:'local-test-password'}})
  fireEvent.click(screen.getByRole('button',{name:'Подтвердить'}))
  await waitFor(()=>expect(api.submitMaintenance).toHaveBeenCalledTimes(1))
  expect(vi.mocked(api.submitMaintenance).mock.calls[0][1]).toMatchObject({action:'strategy_configure',settings:{enabled:true,mode:'auto',interval_minutes:15,daily_enabled:false}})
})

it('read-only admin and blocked runtime cannot start tests or apply',async()=>{
  vi.mocked(api.antidpiStrategies).mockResolvedValue({...snapshot,can_manage:false,capabilities:{can_check:false,can_apply:false,can_configure:false,blockers:['dns_renewal_unverified']}} as never)
  mount()
  expect(await screen.findByRole('button',{name:'Проверить YouTube сейчас'})).toBeDisabled()
  expect(screen.getByRole('button',{name:'Включить авто YouTube'})).toBeDisabled()
  expect(screen.getByText(/Обновление DNS ещё не проверено/)).toBeVisible()
})

it('lost response restores exact job and never repeats submit',async()=>{
  vi.mocked(api.submitMaintenance).mockRejectedValue(new TypeError('Failed to fetch'))
  mount();fireEvent.click(await screen.findByRole('button',{name:'Подобрать для YouTube'}))
  fireEvent.change(screen.getByLabelText('Пароль владельца'),{target:{value:'local-test-password'}})
  fireEvent.click(screen.getByRole('button',{name:'Подтвердить'}))
  expect(await screen.findByText(/Ответ потерян/)).toBeVisible()
  expect(api.submitMaintenance).toHaveBeenCalledTimes(1)
  expect(api.maintenanceJob).toHaveBeenCalled()
})

it('terminal job refreshes strategy snapshot without navigation',async()=>{
  vi.mocked(api.maintenanceJob).mockResolvedValue({job_id:jobId,phase:'completed',cancel_allowed:false})
  mount('/routes/antidpi/strategies?job='+jobId)
  expect(await screen.findByText('Завершено')).toBeVisible()
  await waitFor(()=>expect(vi.mocked(api.antidpiStrategies).mock.calls.length).toBeGreaterThan(1))
  expect(api.submitMaintenance).not.toHaveBeenCalled()
})

it('manual apply explicitly chooses pinned or auto and shows recent success ratio',async()=>{
  mount();await screen.findByText('Текущая: tlsrec-sni')
  expect(screen.getByText('Успешных проб текущей: 0 из 0 последних')).toBeVisible()
  fireEvent.change(screen.getByLabelText('Стратегия YouTube'),{target:{value:'disorder-1'}})
  fireEvent.change(screen.getByLabelText('После применения YouTube'),{target:{value:'auto'}})
  fireEvent.click(screen.getByRole('button',{name:'Применить для YouTube'}))
  expect(within(screen.getByRole('dialog')).getByText(/После применения: Авто/)).toBeVisible()
  fireEvent.change(screen.getByLabelText('Пароль владельца'),{target:{value:'local-test-password'}})
  fireEvent.click(screen.getByRole('button',{name:'Подтвердить'}))
  await waitFor(()=>expect(api.submitMaintenance).toHaveBeenCalledTimes(1))
  expect(vi.mocked(api.submitMaintenance).mock.calls[0][1]).toMatchObject({action:'strategy_apply',settings:{strategy_id:'disorder-1',mode:'auto'}})
})
