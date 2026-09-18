<!--
  SPDX-License-Identifier: GPL-3.0-or-later
-->
<template>
  <cv-grid fullWidth>
    <cv-row>
      <cv-column class="page-title">
        <h2>{{ $t("reports.title") }}</h2>
      </cv-column>
    </cv-row>
    <cv-row v-if="error.listReports">
      <cv-column>
        <NsInlineNotification
          kind="error"
          :title="$t('action.list-reports')"
          :description="error.listReports"
          :showCloseButton="false"
        />
      </cv-column>
    </cv-row>

    <!-- severity summary -->
    <cv-row>
      <cv-column :md="4" :max="4" v-for="card in summaryCards" :key="card.key">
        <cv-tile light class="summary-card" :class="'severity-' + card.key.toLowerCase()">
          <div class="summary-n">{{ summary[card.key] || 0 }}</div>
          <div class="summary-l">{{ card.label }}</div>
        </cv-tile>
      </cv-column>
    </cv-row>

    <!-- filters -->
    <cv-row>
      <cv-column>
        <cv-tile light class="filters-tile">
          <div class="chip-row">
            <button
              v-for="sev in severities"
              :key="sev"
              type="button"
              class="sev-chip"
              :class="[
                'sev-chip--' + sev.toLowerCase(),
                { active: activeSeverities.includes(sev) },
              ]"
              @click="toggleSeverity(sev)"
            >
              <span class="dot"></span>{{ severityLabel(sev) }}
            </button>
            <select v-model="destinationFilter" class="dest-select">
              <option value="">{{ $t("reports.all_destinations") }}</option>
              <option v-for="d in destinations" :key="d" :value="d">
                {{ d }}
              </option>
            </select>
            <input
              type="text"
              class="search-input"
              v-model="searchQuery"
              :placeholder="$t('reports.search_placeholder')"
            />
            <NsButton
              kind="ghost"
              :icon="Renew20"
              :loading="loading.listReports"
              @click="listReports"
              >{{ $t("reports.refresh") }}</NsButton
            >
          </div>
        </cv-tile>
      </cv-column>
    </cv-row>

    <!-- table -->
    <cv-row>
      <cv-column>
        <cv-tile light class="table-tile">
          <div v-if="loading.listReports && !hasLoadedOnce">
            <cv-skeleton-text :paragraph="true" :line-count="6"></cv-skeleton-text>
          </div>
          <NsEmptyState
            v-else-if="filteredReports.length === 0"
            :title="
              reports.length
                ? $t('reports.no_match_title')
                : $t('reports.no_reports_title')
            "
          >
          </NsEmptyState>
          <table v-else class="reports-table">
            <thead>
              <tr>
                <th>{{ $t("reports.col_severity") }}</th>
                <th>{{ $t("reports.col_time") }}</th>
                <th>{{ $t("reports.col_user") }}</th>
                <th>{{ $t("reports.col_set") }}</th>
                <th>{{ $t("reports.col_destination") }}</th>
                <th>{{ $t("reports.col_status") }}</th>
                <th>{{ $t("reports.col_size") }}</th>
              </tr>
            </thead>
            <tbody>
              <tr
                v-for="r in filteredReports"
                :key="r.natural_key"
                @click="openDetail(r)"
                class="report-row"
              >
                <td>
                  <span
                    class="sev-badge"
                    :class="'sev-badge--' + r.severity.toLowerCase()"
                    >{{ severityLabel(r.severity) }}</span
                  >
                </td>
                <td class="mono">{{ r.timestamp || "—" }}</td>
                <td class="strong">{{ r.user || "—" }}</td>
                <td>{{ r.backup_set || "—" }}</td>
                <td class="mono">{{ r.destination || "—" }}</td>
                <td class="muted">{{ r.status || "—" }}</td>
                <td class="mono">{{ r.data_size || "—" }}</td>
              </tr>
            </tbody>
          </table>
        </cv-tile>
      </cv-column>
    </cv-row>

    <!-- detail modal -->
    <cv-modal
      :visible="!!selectedReport"
      @modal-hidden="selectedReport = null"
      v-if="selectedReport"
    >
      <template slot="title">{{ selectedReport.user }} — {{ selectedReport.backup_set }}</template>
      <template slot="content">
        <p class="detail-status" :class="'sev-badge--' + selectedReport.severity.toLowerCase()">
          {{ selectedReport.status || "—" }}
        </p>
        <dl class="detail-list">
          <dt>{{ $t("reports.col_destination") }}</dt>
          <dd class="mono">{{ selectedReport.destination || "—" }}</dd>
          <dt>{{ $t("reports.col_size") }}</dt>
          <dd class="mono">{{ selectedReport.data_size || "—" }}</dd>
          <dt>{{ $t("reports.job_id") }}</dt>
          <dd class="mono">{{ selectedReport.job_id || "—" }}</dd>
          <dt>{{ $t("reports.period") }}</dt>
          <dd class="mono">{{ selectedReport.start_end || "—" }}</dd>
          <dt>{{ $t("reports.ip_address") }}</dt>
          <dd class="mono">{{ selectedReport.ip_address || "—" }}</dd>
          <dt>{{ $t("reports.source") }}</dt>
          <dd>{{ selectedReport.source || "—" }}</dd>
        </dl>
      </template>
    </cv-modal>
  </cv-grid>
</template>

<script>
import to from "await-to-js";
import { mapState } from "vuex";
import Renew20 from "@carbon/icons-vue/es/renew/20";
import {
  QueryParamService,
  UtilService,
  TaskService,
  IconService,
  PageTitleService,
} from "@nethserver/ns8-ui-lib";

const SEVERITIES = ["CRITICAL", "WARNING", "OK", "INFO"];
let refreshTimer = null;

export default {
  name: "Reports",
  mixins: [
    TaskService,
    IconService,
    UtilService,
    QueryParamService,
    PageTitleService,
  ],
  pageTitle() {
    return this.$t("reports.title") + " - " + this.appName;
  },
  data() {
    return {
      Renew20,
      q: {
        page: "status",
      },
      urlCheckInterval: null,
      severities: SEVERITIES,
      activeSeverities: [...SEVERITIES],
      destinationFilter: "",
      searchQuery: "",
      reports: [],
      destinations: [],
      summary: { CRITICAL: 0, WARNING: 0, OK: 0, INFO: 0 },
      selectedReport: null,
      hasLoadedOnce: false,
      loading: { listReports: false },
      error: { listReports: "" },
    };
  },
  computed: {
    ...mapState(["instanceName", "core", "appName"]),
    summaryCards() {
      return [
        { key: "CRITICAL", label: this.$t("reports.severity_critical") },
        { key: "WARNING", label: this.$t("reports.severity_warning") },
        { key: "OK", label: this.$t("reports.severity_ok") },
        { key: "INFO", label: this.$t("reports.severity_info") },
      ];
    },
    filteredReports() {
      const q = this.searchQuery.trim().toLowerCase();
      return this.reports.filter((r) => {
        if (!this.activeSeverities.includes(r.severity)) return false;
        if (this.destinationFilter && r.destination !== this.destinationFilter)
          return false;
        if (
          q &&
          !(r.user || "").toLowerCase().includes(q) &&
          !(r.backup_set || "").toLowerCase().includes(q)
        )
          return false;
        return true;
      });
    },
  },
  beforeRouteEnter(to, from, next) {
    next((vm) => {
      vm.watchQueryData(vm);
      vm.urlCheckInterval = vm.initUrlBindingForApp(vm, vm.q.page);
    });
  },
  beforeRouteLeave(to, from, next) {
    clearInterval(this.urlCheckInterval);
    if (refreshTimer) clearInterval(refreshTimer);
    next();
  },
  created() {
    this.listReports();
    refreshTimer = setInterval(() => this.listReports(), 60000);
  },
  methods: {
    severityLabel(sev) {
      return this.$t("reports.severity_" + sev.toLowerCase());
    },
    toggleSeverity(sev) {
      const idx = this.activeSeverities.indexOf(sev);
      if (idx >= 0) this.activeSeverities.splice(idx, 1);
      else this.activeSeverities.push(sev);
    },
    openDetail(report) {
      this.selectedReport = report;
    },
    async listReports() {
      this.loading.listReports = true;
      this.error.listReports = "";
      const taskAction = "list-reports";
      const eventId = this.getUuid();

      this.core.$root.$once(
        `${taskAction}-aborted-${eventId}`,
        this.listReportsAborted
      );
      this.core.$root.$once(
        `${taskAction}-completed-${eventId}`,
        this.listReportsCompleted
      );

      const res = await to(
        this.createModuleTaskForApp(this.instanceName, {
          action: taskAction,
          data: {},
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
        this.error.listReports = this.getErrorMessage(err);
        this.loading.listReports = false;
        return;
      }
    },
    listReportsAborted(taskResult, taskContext) {
      console.error(`${taskContext.action} aborted`, taskResult);
      this.error.listReports = this.$t("error.generic_error");
      this.loading.listReports = false;
    },
    listReportsCompleted(taskContext, taskResult) {
      const output = taskResult.output || {};
      this.reports = output.reports || [];
      this.summary = output.summary || { CRITICAL: 0, WARNING: 0, OK: 0, INFO: 0 };
      this.destinations = output.destinations || [];
      this.loading.listReports = false;
      this.hasLoadedOnce = true;
    },
  },
};
</script>

<style scoped lang="scss">
@import "../styles/carbon-utils";

.summary-card {
  border-left: 3px solid #c6c6c6;
  .summary-n {
    font-size: 2rem;
    font-weight: 600;
    font-family: monospace;
  }
  .summary-l {
    font-size: 0.75rem;
    color: #6f6f6f;
    margin-top: 0.25rem;
  }
  &.severity-critical {
    border-left-color: #da1e28;
    .summary-n {
      color: #da1e28;
    }
  }
  &.severity-warning {
    border-left-color: #b28600;
    .summary-n {
      color: #b28600;
    }
  }
  &.severity-ok {
    border-left-color: #198038;
    .summary-n {
      color: #198038;
    }
  }
}

.filters-tile {
  .chip-row {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 0.5rem;
  }
}

.sev-chip {
  display: inline-flex;
  align-items: center;
  gap: 0.4rem;
  border: 1px solid #c6c6c6;
  background: #f4f4f4;
  border-radius: 1rem;
  padding: 0.25rem 0.75rem;
  font-size: 0.75rem;
  cursor: pointer;
  color: #6f6f6f;

  .dot {
    width: 7px;
    height: 7px;
    border-radius: 50%;
    background: currentColor;
  }

  &--critical.active {
    background: #fff1f1;
    color: #da1e28;
    border-color: #da1e28;
  }
  &--warning.active {
    background: #fcf4d6;
    color: #b28600;
    border-color: #b28600;
  }
  &--ok.active {
    background: #defbe6;
    color: #198038;
    border-color: #198038;
  }
  &--info.active {
    background: #edf5ff;
    color: #0043ce;
    border-color: #0043ce;
  }
}

.dest-select,
.search-input {
  font-size: 0.8rem;
  padding: 0.35rem 0.6rem;
  border: 1px solid #c6c6c6;
  border-radius: 0.25rem;
}

.reports-table {
  width: 100%;
  border-collapse: collapse;
  font-size: 0.85rem;

  th {
    text-align: left;
    font-size: 0.7rem;
    color: #6f6f6f;
    padding: 0.5rem 0.75rem;
    border-bottom: 1px solid #e0e0e0;
    white-space: nowrap;
  }
  td {
    padding: 0.6rem 0.75rem;
    border-bottom: 1px solid #e0e0e0;
    vertical-align: top;
  }
  .report-row {
    cursor: pointer;
  }
  .report-row:hover {
    background: #f4f4f4;
  }
  .mono {
    font-family: monospace;
    font-size: 0.8rem;
    color: #525252;
  }
  .strong {
    font-weight: 600;
  }
  .muted {
    color: #6f6f6f;
    max-width: 320px;
  }
}

.sev-badge {
  font-size: 0.7rem;
  font-weight: 600;
  padding: 0.15rem 0.5rem;
  border-radius: 0.25rem;
  white-space: nowrap;
  &--critical {
    background: #fff1f1;
    color: #da1e28;
  }
  &--warning {
    background: #fcf4d6;
    color: #b28600;
  }
  &--ok {
    background: #defbe6;
    color: #198038;
  }
  &--info {
    background: #edf5ff;
    color: #0043ce;
  }
}

.detail-status {
  display: inline-block;
  padding: 0.4rem 0.75rem;
  border-radius: 0.25rem;
  margin-bottom: 1rem;
}

.detail-list {
  display: grid;
  grid-template-columns: auto 1fr;
  gap: 0.4rem 1rem;
  dt {
    color: #6f6f6f;
    font-size: 0.8rem;
  }
  dd {
    margin: 0;
    font-size: 0.85rem;
  }
}
</style>
