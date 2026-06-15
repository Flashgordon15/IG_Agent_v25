/*
 * Minimal Mach-O entry point for IG Agent v29.0.app.
 * macOS LaunchServices requires a native executable; this runs launch.sh.
 *
 * Fork + wait (instead of execv) keeps the .app registered briefly after
 * launch.sh exits so Finder does not show "application is not open anymore".
 */
#include <limits.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <sys/wait.h>
#include <mach-o/dyld.h>

int main(int argc, char *argv[]) {
    char exe[PATH_MAX];
    uint32_t size = sizeof(exe);
    if (_NSGetExecutablePath(exe, &size) != 0) {
        return 1;
    }

    char *macos = strrchr(exe, '/');
    if (!macos) return 1;
    *macos = '\0';

    char *contents = strrchr(exe, '/');
    if (!contents) return 1;
    *contents = '\0';

    char script[PATH_MAX];
    snprintf(script, sizeof(script), "%s/Resources/launch.sh", exe);

    pid_t pid = fork();
    if (pid < 0) {
        return 1;
    }
    if (pid == 0) {
        char *args[] = {"/bin/bash", script, NULL};
        execv("/bin/bash", args);
        _exit(1);
    }

    int status = 0;
    if (waitpid(pid, &status, 0) < 0) {
        return 1;
    }

    /* Let LaunchServices settle before the .app process exits. */
    sleep(2);

    if (WIFEXITED(status)) {
        return WEXITSTATUS(status);
    }
    return 1;
}
