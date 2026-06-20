/*
 * IG Agent Apex — macOS IOPMAssertion bridge (prevents App Nap / idle sleep).
 * Build: cc -framework IOKit -framework CoreFoundation -o no_nap no_nap.c
 */
#include <CoreFoundation/CoreFoundation.h>
#include <IOKit/pwr_mgt/IOPMLib.h>
#include <signal.h>
#include <unistd.h>

static IOPMAssertionID assertion_id = 0;

static void on_signal(int sig) {
  (void)sig;
  if (assertion_id != 0) {
    IOPMAssertionRelease(assertion_id);
    assertion_id = 0;
  }
  _exit(0);
}

int main(void) {
  IOReturn rc = IOPMAssertionCreateWithName(
      CFSTR("PreventUserIdleSystemSleep"),
      kIOPMAssertionLevelOn,
      CFSTR("IG Agent Apex Live Session"),
      &assertion_id);
  if (rc != kIOReturnSuccess) {
    return 1;
  }
  signal(SIGTERM, on_signal);
  signal(SIGINT, on_signal);
  for (;;) {
    sleep(3600);
  }
  return 0;
}
