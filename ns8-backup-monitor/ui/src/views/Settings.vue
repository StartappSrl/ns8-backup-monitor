<!--
  SPDX-License-Identifier: GPL-3.0-or-later
-->
<template>
  <cv-grid fullWidth>
    <cv-row>
      <cv-column class="page-title">
        <h2>{{ $t("settings.title") }}</h2>
      </cv-column>
    </cv-row>
    <cv-row v-if="error.getConfiguration">
      <cv-column>
        <NsInlineNotification
          kind="error"
          :title="$t('action.get-configuration')"
          :description="error.getConfiguration"
          :showCloseButton="false"
        />
      </cv-column>
    </cv-row>
    <cv-row>
      <cv-column>
        <cv-tile light>
          <cv-form @submit.prevent="configureModule">
            <cv-text-input
              :label="$t('settings.imap_host')"
              v-model="imapHost"
              placeholder="mail.example.org"
              :disabled="loading.getConfiguration || loading.configureModule"
              :invalid-message="error.imapHost"
              ref="imapHost"
            ></cv-text-input>
            <cv-text-input
              :label="$t('settings.imap_port')"
              v-model="imapPort"
              type="number"
              placeholder="993"
              :disabled="loading.getConfiguration || loading.configureModule"
              :invalid-message="error.imapPort"
              ref="imapPort"
            ></cv-text-input>
            <cv-checkbox
              :label="$t('settings.imap_ssl')"
              v-model="imapSsl"
              :disabled="loading.getConfiguration || loading.configureModule"
            ></cv-checkbox>
            <cv-text-input
              :label="$t('settings.imap_username')"
              v-model="imapUsername"
              placeholder="support@example.org"
              :disabled="loading.getConfiguration || loading.configureModule"
              :invalid-message="error.imapUsername"
              ref="imapUsername"
            ></cv-text-input>
            <cv-text-input
              :label="$t('settings.imap_password')"
              v-model="imapPassword"
              type="password"
              :placeholder="
                imapPasswordSet
                  ? $t('settings.imap_password_unchanged')
                  : ''
              "
              :disabled="loading.getConfiguration || loading.configureModule"
              :invalid-message="error.imapPassword"
              ref="imapPassword"
            ></cv-text-input>
            <label class="list-label">{{ $t('settings.imap_folders') }}</label>
            <p class="list-helper">{{ $t('settings.imap_folders_helper') }}</p>
            <div class="dynamic-row" v-for="(f, idx) in imapFolders" :key="'folder-' + idx">
              <cv-text-input
                :label="''"
                v-model="imapFolders[idx]"
                placeholder="INBOX"
                :disabled="loading.getConfiguration || loading.configureModule"
              ></cv-text-input>
              <button
                type="button"
                class="remove-row-btn"
                :disabled="imapFolders.length <= 1"
                @click="imapFolders.splice(idx, 1)"
              >&times;</button>
            </div>
            <NsButton kind="ghost" size="small" :icon="Add16" @click="imapFolders.push('')" type="button">
              {{ $t('settings.add_folder') }}
            </NsButton>

            <label class="list-label" style="margin-top:24px;">{{ $t('settings.sender_filters') }}</label>
            <p class="list-helper">{{ $t('settings.sender_filters_helper') }}</p>
            <div class="dynamic-row" v-for="(s, idx) in senderFilters" :key="'sender-' + idx">
              <cv-text-input
                :label="''"
                v-model="senderFilters[idx]"
                placeholder="backup@1backup.it"
                :disabled="loading.getConfiguration || loading.configureModule"
              ></cv-text-input>
              <button
                type="button"
                class="remove-row-btn"
                :disabled="senderFilters.length <= 1"
                @click="senderFilters.splice(idx, 1)"
              >&times;</button>
            </div>
            <NsButton kind="ghost" size="small" :icon="Add16" @click="senderFilters.push('')" type="button">
              {{ $t('settings.add_sender') }}
            </NsButton>
            <div v-if="error.senderFilters" class="field-error">{{ error.senderFilters }}</div>

            <cv-text-input
              :label="$t('settings.poll_interval')"
              :helper-text="$t('settings.poll_interval_helper')"
              v-model="pollInterval"
              type="number"
              placeholder="300"
              :disabled="loading.getConfiguration || loading.configureModule"
            ></cv-text-input>
            <cv-text-input
              :label="$t('settings.retention_days')"
              :helper-text="$t('settings.retention_days_helper')"
              v-model="retentionDays"
              type="number"
              placeholder="15"
              :disabled="loading.getConfiguration || loading.configureModule"
            ></cv-text-input>
            <cv-text-input
              :label="$t('settings.fqdn')"
              :helper-text="$t('settings.fqdn_helper')"
              v-model="fqdn"
              placeholder="backup-monitor.example.org"
              :disabled="loading.getConfiguration || loading.configureModule"
              :invalid-message="error.fqdn"
              ref="fqdn"
            ></cv-text-input>
            <cv-row v-if="error.configureModule">
              <cv-column>
                <NsInlineNotification
                  kind="error"
                  :title="$t('action.configure-module')"
                  :description="error.configureModule"
                  :showCloseButton="false"
                />
              </cv-column>
            </cv-row>
            <NsButton
              kind="primary"
              :icon="Save20"
              :loading="loading.configureModule"
              :disabled="loading.getConfiguration || loading.configureModule"
              >{{ $t("settings.save") }}</NsButton
            >
          </cv-form>
        </cv-tile>
      </cv-column>
    </cv-row>
  </cv-grid>
</template>

<script>
import to from "await-to-js";
import { mapState } from "vuex";
import Save20 from "@carbon/icons-vue/es/save/20";
import Add16 from "@carbon/icons-vue/es/add/16";
import {
  QueryParamService,
  UtilService,
  TaskService,
  IconService,
  PageTitleService,
} from "@nethserver/ns8-ui-lib";

export default {
  name: "Settings",
  mixins: [
    TaskService,
    IconService,
    UtilService,
    QueryParamService,
    PageTitleService,
  ],
  pageTitle() {
    return this.$t("settings.title") + " - " + this.appName;
  },
  data() {
    return {
      Save20,
      Add16,
      q: {
        page: "settings",
      },
      urlCheckInterval: null,
      imapHost: "",
      imapPort: "993",
      imapSsl: true,
      imapUsername: "",
      imapPassword: "",
      imapPasswordSet: false,
      imapFolders: ["INBOX"],
      senderFilters: [""],
      pollInterval: "300",
      retentionDays: "15",
      fqdn: "",
      loading: {
        getConfiguration: false,
        configureModule: false,
      },
      error: {
        getConfiguration: "",
        configureModule: "",
        imapHost: "",
        imapPort: "",
        imapUsername: "",
        imapPassword: "",
        senderFilters: "",
      },
    };
  },
  computed: {
    ...mapState(["instanceName", "core", "appName"]),
  },
  beforeRouteEnter(to, from, next) {
    next((vm) => {
      vm.watchQueryData(vm);
      vm.urlCheckInterval = vm.initUrlBindingForApp(vm, vm.q.page);
    });
  },
  beforeRouteLeave(to, from, next) {
    clearInterval(this.urlCheckInterval);
    next();
  },
  created() {
    this.getConfiguration();
  },
  methods: {
    async getConfiguration() {
      this.loading.getConfiguration = true;
      this.error.getConfiguration = "";
      const taskAction = "get-configuration";
      const eventId = this.getUuid();

      this.core.$root.$once(
        `${taskAction}-aborted-${eventId}`,
        this.getConfigurationAborted
      );
      this.core.$root.$once(
        `${taskAction}-completed-${eventId}`,
        this.getConfigurationCompleted
      );

      const res = await to(
        this.createModuleTaskForApp(this.instanceName, {
          action: taskAction,
          extra: {
            title: this.$t("action." + taskAction),
            isNotificationHidden: true,
            eventId,
          },
        })
      );
      const err = res[0];

      if (err) {
        console.error(`error creating task ${taskAction}`, err);
        this.error.getConfiguration = this.getErrorMessage(err);
        this.loading.getConfiguration = false;
        return;
      }
    },
    getConfigurationAborted(taskResult, taskContext) {
      console.error(`${taskContext.action} aborted`, taskResult);
      this.error.getConfiguration = this.$t("error.generic_error");
      this.loading.getConfiguration = false;
    },
    getConfigurationCompleted(taskContext, taskResult) {
      this.loading.getConfiguration = false;
      const config = taskResult.output;

      this.imapHost = config.imap_host || "";
      this.imapPort = String(config.imap_port || 993);
      this.imapSsl = config.imap_ssl !== false;
      this.imapUsername = config.imap_username || "";
      this.imapPasswordSet = !!config.imap_password_set;
      this.imapFolders = (config.imap_folders && config.imap_folders.length) ? config.imap_folders.slice() : ["INBOX"];
      this.senderFilters = (config.sender_filters && config.sender_filters.length) ? config.sender_filters.slice() : [""];
      this.pollInterval = String(config.poll_interval || 300);
      this.retentionDays = String(config.retention_days || 15);
      this.fqdn = config.fqdn || "";

      this.focusElement("imapHost");
    },
    validateConfigureModule() {
      this.clearErrors(this);
      let isValidationOk = true;
      let focusAlreadySet = false;

      const require = (field, ref) => {
        if (!this[field]) {
          this.error[field] = this.$t("common.required");
          if (!focusAlreadySet) {
            this.focusElement(ref);
            focusAlreadySet = true;
          }
          isValidationOk = false;
        }
      };

      require("imapHost", "imapHost");
      require("imapUsername", "imapUsername");
      const nonEmptySenders = this.senderFilters.map(s => s.trim()).filter(Boolean);
      if (nonEmptySenders.length === 0) {
        this.error.senderFilters = this.$t("common.required");
        if (!focusAlreadySet) focusAlreadySet = true;
        isValidationOk = false;
      } else {
        this.error.senderFilters = "";
      }
      require("fqdn", "fqdn");

      if (!this.imapPasswordSet && !this.imapPassword) {
        this.error.imapPassword = this.$t("common.required");
        if (!focusAlreadySet) {
          this.focusElement("imapPassword");
          focusAlreadySet = true;
        }
        isValidationOk = false;
      }

      return isValidationOk;
    },
    configureModuleValidationFailed(validationErrors) {
      this.loading.configureModule = false;
      let focusAlreadySet = false;

      for (const validationError of validationErrors) {
        const field = validationError.field;

        if (field !== "(root)") {
          this.error[field] = this.$t("settings." + validationError.error);

          if (!focusAlreadySet) {
            this.focusElement(field);
            focusAlreadySet = true;
          }
        }
      }
    },
    async configureModule() {
      const isValidationOk = this.validateConfigureModule();
      if (!isValidationOk) {
        return;
      }

      this.loading.configureModule = true;
      const taskAction = "configure-module";
      const eventId = this.getUuid();

      this.core.$root.$once(
        `${taskAction}-aborted-${eventId}`,
        this.configureModuleAborted
      );
      this.core.$root.$once(
        `${taskAction}-validation-failed-${eventId}`,
        this.configureModuleValidationFailed
      );
      this.core.$root.$once(
        `${taskAction}-completed-${eventId}`,
        this.configureModuleCompleted
      );

      const res = await to(
        this.createModuleTaskForApp(this.instanceName, {
          action: taskAction,
          data: {
            imap_host: this.imapHost,
            imap_port: parseInt(this.imapPort, 10) || 993,
            imap_ssl: this.imapSsl,
            imap_username: this.imapUsername,
            ...(this.imapPassword
              ? { imap_password: this.imapPassword }
              : {}),
            imap_folders: this.imapFolders.map(f => f.trim()).filter(Boolean),
            sender_filters: this.senderFilters.map(s => s.trim()).filter(Boolean),
            poll_interval: parseInt(this.pollInterval, 10) || 300,
            retention_days: parseInt(this.retentionDays, 10) || 15,
            fqdn: this.fqdn,
          },
          extra: {
            title: this.$t("settings.configure_instance", {
              instance: this.instanceName,
            }),
            description: this.$t("common.processing"),
            eventId,
          },
        })
      );
      const err = res[0];

      if (err) {
        console.error(`error creating task ${taskAction}`, err);
        this.error.configureModule = this.getErrorMessage(err);
        this.loading.configureModule = false;
        return;
      }
    },
    configureModuleAborted(taskResult, taskContext) {
      console.error(`${taskContext.action} aborted`, taskResult);
      this.error.configureModule = this.$t("error.generic_error");
      this.loading.configureModule = false;
    },
    configureModuleCompleted() {
      this.loading.configureModule = false;
      this.imapPassword = "";
      this.getConfiguration();
    },
  },
};
</script>

<style scoped lang="scss">
@import "../styles/carbon-utils";

.list-label {
  font-size: 0.75rem;
  font-weight: 400;
  color: #525252;
  margin-bottom: 2px;
}
.list-helper {
  font-size: 0.75rem;
  color: #6f6f6f;
  margin: 0 0 8px 0;
}
.dynamic-row {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 6px;
  .bx--form-item {
    flex: 1;
  }
}
.remove-row-btn {
  border: none;
  background: transparent;
  color: #6f6f6f;
  font-size: 18px;
  line-height: 1;
  cursor: pointer;
  padding: 4px 8px;
  &:disabled {
    opacity: 0.3;
    cursor: not-allowed;
  }
}
.field-error {
  color: #da1e28;
  font-size: 0.75rem;
  margin-top: 4px;
}
</style>
