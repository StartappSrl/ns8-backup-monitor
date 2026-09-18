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
            <cv-text-input
              :label="$t('settings.imap_folder')"
              v-model="imapFolder"
              placeholder="INBOX"
              :disabled="loading.getConfiguration || loading.configureModule"
            ></cv-text-input>
            <cv-text-input
              :label="$t('settings.sender_filter')"
              :helper-text="$t('settings.sender_filter_helper')"
              v-model="senderFilter"
              placeholder="backup@1backup.it"
              :disabled="loading.getConfiguration || loading.configureModule"
              :invalid-message="error.senderFilter"
              ref="senderFilter"
            ></cv-text-input>
            <cv-text-input
              :label="$t('settings.poll_interval')"
              :helper-text="$t('settings.poll_interval_helper')"
              v-model="pollInterval"
              type="number"
              placeholder="300"
              :disabled="loading.getConfiguration || loading.configureModule"
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
      imapFolder: "INBOX",
      senderFilter: "",
      pollInterval: "300",
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
        senderFilter: "",
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
      this.imapFolder = config.imap_folder || "INBOX";
      this.senderFilter = config.sender_filter || "";
      this.pollInterval = String(config.poll_interval || 300);

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
      require("senderFilter", "senderFilter");

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
            imap_folder: this.imapFolder || "INBOX",
            sender_filter: this.senderFilter,
            poll_interval: parseInt(this.pollInterval, 10) || 300,
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
</style>
