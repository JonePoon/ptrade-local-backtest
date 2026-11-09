/** A single owner for one UI request stream; cancellation invalidates old responses. */
export class RequestSlot {
  private controller: AbortController | null = null

  begin(): AbortController {
    this.cancel()
    this.controller = new AbortController()
    return this.controller
  }

  current(controller: AbortController): boolean {
    return this.controller === controller && !controller.signal.aborted
  }

  cancel(): void {
    this.controller?.abort()
    this.controller = null
  }
}
