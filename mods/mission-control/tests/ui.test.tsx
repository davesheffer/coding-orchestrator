import { expect, test } from 'claude-code/testing'

const SURFACES = ['terminal', 'desktop', 'vscode'] as const

const scroll = { offset: 0, bodyRows: 20 }

for (const surface of SURFACES) {
  test(`the band draws the zone and hides on its button (${surface})`, async ($, on) => {
    // Stand-ins for the engine beneath the plugin: its own (empty) band and the status line.
    on('ui.render', { component: 'AbovePrompt' }, ($, e) => {
      const { Box } = $.ui.resolve(e)

      return <Box key="engine-band" />
    })
    on('ui.status', () => ({ value: undefined }))

    const ui = await $.ui.mount({
      plugin: 'mission-control',
      surface,
      component: 'AbovePrompt',
      props: { hasSurvey: false, isWorking: false, maxRows: 6, bodyColumns: 120, scroll, view: {} },
    })
    expect(await ui.find({ text: /UNKNOWN/ })).toBeDefined()
    expect(await ui.find({ key: 'open' })).toBeDefined()

    await ui.press({ key: 'hide' })
    expect(await ui.find({ text: /UNKNOWN/ })).toBeUndefined()
    expect(await ui.find({ key: 'engine-band' })).toBeDefined()

    await ui.unmount()
  })
}

test('the pane draws its sections with nothing yet recorded', async $ => {
  for (const surface of SURFACES) {
    const ui = await $.ui.mount({
      plugin: 'mission-control',
      surface,
      component: 'Pane',
      requestId: 'mission-control',
      props: { title: 'Mission Control', isFocused: false, bodyColumns: 60, placement: 'dock', scroll, view: {} },
    })
    expect(await ui.find({ text: /idle, waiting for you/ })).toBeDefined()
    expect(await ui.find({ text: /Nothing yet/ })).toBeDefined()
    expect(await ui.find({ text: /No subagents running/ })).toBeUndefined()
    expect(await ui.find({ key: 'band' })).toBeDefined()

    await ui.press({ key: 'details' })
    expect(await ui.find({ text: /No subagents running/ })).toBeDefined()
    expect(await ui.find({ text: /No role reports yet/ })).toBeDefined()
    await ui.press({ key: 'details' })

    await ui.unmount()
  }
})
