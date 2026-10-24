#include <errno.h>
#include <stddef.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

#define CONTROL_ERROR 125
#define EXEC_ERROR 126
#define MAX_ARGUMENTS 512
#define MAX_ARGUMENT_BYTES (128U * 1024U)

static const char marker[] = "PERSONAL_OPS_AGENT_BOOTSTRAP_V1\n";
static const char *allowed_keys[] = {
    "HOME",
    "CODEX_HOME",
    "PATH",
    "LANG",
    "LC_ALL",
    "SSL_CERT_FILE",
    "GIT_CONFIG_NOSYSTEM",
    "GIT_CONFIG_GLOBAL",
};

static void control_error(void) {
    _exit(CONTROL_ERROR);
}

static int allowed_key(const char *assignment, size_t key_length) {
    size_t count = sizeof(allowed_keys) / sizeof(allowed_keys[0]);
    for (size_t index = 0; index < count; ++index) {
        if (strlen(allowed_keys[index]) == key_length &&
                memcmp(assignment, allowed_keys[index], key_length) == 0) {
            return (int)index;
        }
    }
    return -1;
}

static void write_all(int descriptor, const char *data, size_t length) {
    size_t written = 0;
    while (written < length) {
        ssize_t result = write(descriptor, data + written, length - written);
        if (result < 0 && errno == EINTR) {
            continue;
        }
        if (result <= 0) {
            control_error();
        }
        written += (size_t)result;
    }
}

static void wait_for_go(void) {
    char handshake[3];
    size_t received = 0;
    while (received < sizeof(handshake)) {
        ssize_t result = read(STDIN_FILENO, handshake + received,
                              sizeof(handshake) - received);
        if (result < 0 && errno == EINTR) {
            continue;
        }
        if (result <= 0) {
            control_error();
        }
        received += (size_t)result;
    }
    if (memcmp(handshake, "GO\n", sizeof(handshake)) != 0) {
        control_error();
    }
}

int main(int argc, char **argv) {
    const size_t allowed_count = sizeof(allowed_keys) / sizeof(allowed_keys[0]);
    char *child_environment[(sizeof(allowed_keys) / sizeof(allowed_keys[0])) + 1];
    unsigned char seen[sizeof(allowed_keys) / sizeof(allowed_keys[0])] = {0};
    size_t environment_count = 0;
    size_t total_bytes = 0;
    const char *supplied_home = NULL;
    int separator = -1;

    if (argc < 4 || argc > MAX_ARGUMENTS) {
        control_error();
    }
    for (int index = 1; index < argc; ++index) {
        size_t length = strlen(argv[index]);
        if (length >= MAX_ARGUMENT_BYTES || total_bytes > MAX_ARGUMENT_BYTES - length - 1) {
            control_error();
        }
        total_bytes += length + 1;
        if (strcmp(argv[index], "--") == 0) {
            separator = index;
            break;
        }
        const char *equals = strchr(argv[index], '=');
        if (equals == NULL || equals == argv[index]) {
            control_error();
        }
        size_t key_length = (size_t)(equals - argv[index]);
        int key = allowed_key(argv[index], key_length);
        if (key < 0 || seen[key] || environment_count >= allowed_count) {
            control_error();
        }
        seen[key] = 1;
        if (key == 0) {
            supplied_home = equals + 1;
        }
        child_environment[environment_count++] = argv[index];
    }
    if (separator < 0 || separator + 1 >= argc || !seen[0]) {
        control_error();
    }
    const char *inherited_home = getenv("HOME");
    if (inherited_home == NULL || supplied_home == NULL || inherited_home[0] != '/' ||
            supplied_home[0] != '/' ||
            strcmp(inherited_home, supplied_home) != 0) {
        control_error();
    }
    const char *program = argv[separator + 1];
    if (program[0] != '/') {
        control_error();
    }
    for (int index = separator + 1; index < argc; ++index) {
        size_t length = strlen(argv[index]);
        if (length >= MAX_ARGUMENT_BYTES || total_bytes > MAX_ARGUMENT_BYTES - length - 1) {
            control_error();
        }
        total_bytes += length + 1;
    }
    child_environment[environment_count] = NULL;

    write_all(STDOUT_FILENO, marker, sizeof(marker) - 1);
    wait_for_go();
    execve(program, &argv[separator + 1], child_environment);
    _exit(EXEC_ERROR);
}
