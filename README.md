# netCI Delivery Platform

[![License](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](LICENSE)

netCI is open-source software licensed under Apache-2.0. See
[CONTRIBUTING.md](CONTRIBUTING.md), [SECURITY.md](SECURITY.md) and the
[third-party inventory](THIRD_PARTY_NOTICES.md) before contributing or
redistributing a release.

> Live-data policy: a normal installation starts empty and never substitutes demo
> systems, servers, pipeline history or metrics. Set `NETCI_DEMO_DATA=true` only for
> an explicit demo/test run. Acceptance evidence checked into `evidence/` records a
> historical execution; run the release profile again to certify the current commit.

Local reference implementation của một delivery platform API-first: developer khai báo application từ Custom Portal hoặc Backstage, Jenkins thực hiện CI trên agent ephemeral, còn netCI/Temporal điều phối CD tới Docker, Kubernetes hoặc Systemd.

> Evidence đã commit cho thấy một lần chạy trước đây hoàn tất **11 acceptance gate** trên Ubuntu 24.04. Đây không phải lời chứng nhận tự động cho commit hiện tại: `release-checklist.yaml` định nghĩa gate, còn một lần chạy release profile mới mới là bằng chứng hiện hành. Nếu thiếu Docker/Linux/credential, gate phải báo blocked/fail thay vì kế thừa màu xanh cũ.

## Output cần bàn giao

Một bản hoàn chỉnh phải chứng minh được:

- Reproducible: controller, plugin và job được dựng lại từ Git/JCasC.
- Isolated: mỗi build có agent/workspace riêng và cleanup ở cả success/failure/cancel.
- Extensible: cùng domain contract deploy được qua Docker, Kubernetes và Systemd adapter.
- Governed: artifact bất biến có SBOM, scan, signature, approval, audit và rollback.
- Measurable: có source event, DORA metrics, benchmark và failure-drill evidence.

Luồng chính:

```text
Portal / Backstage -> netCI API -> Jenkins Router -> Jenkins A/B
                                            -> ephemeral CI agent
                                            -> immutable artifact digest
                                            -> policy + approval
                                            -> Docker | Kubernetes | Systemd
```

## Gate nào đã chạy thật

Mỗi gate dưới đây chạy command thật trên hạ tầng thật và ghi `evidence/<gate>.json` gồm command, timestamp, exit code, output và từng assertion kèm verdict. Gate exit non-zero khi một assertion fail.

| Gate | Chứng minh điều gì |
|---|---|
| `make kind-up` | Cluster build có namespace, RBAC controller chỉ giới hạn ở pod trong `netci-build`, không service account nào tự mount token, không có cluster-admin |
| `make security-test` | Artifact đã ký + scan sạch được cho phép; ba trường hợp bị từ chối: có CVE HIGH/CRITICAL có bản vá, thiếu chữ ký, evidence thuộc digest khác |
| `make e2e-container` | Build → registry digest → SBOM/scan/sign → policy → approval → Ansible deploy → health → promote → rollback, với digest đang chạy được đọc lại từ service |
| `make e2e-kubernetes` | Cùng digest được promote lên kind qua Helm; image của pod được đọc lại từ cluster; rollback dùng Helm revision, không build lại |
| `make e2e-systemd` | Binary Go đã ký → unit systemd → symlink `current` → restart → health → rollback, kèm so khớp digest của binary đã cài |
| `make dora-dashboard` | Bốn metric được tính lại độc lập từ `/delivery-events` và so với dashboard; recovery gắn đúng deployment đã fail |

Ngoài ra `make test-durability` chạy trên PostgreSQL thật: khôi phục sau restart, event bền vững, idempotency sống qua restart, và hai process cùng ghi một transition thì chỉ một thắng.

## Những gì chạy được trên Windows

Windows là môi trường phát triển portable, không phải môi trường nghiệm thu runtime Linux.

```powershell
py -3 scripts/doctor.py --profile windows
py -3 scripts/validate_release.py --profile windows --execute
```

Gate Windows kiểm tra Git/Python/Node/npm, syntax và schema, catalog invariant, frontend component tests/typecheck/build, backend unit/contract tests, tài liệu và tính nhất quán của release checklist. Xem [QUICKSTART.md](QUICKSTART.md) để chạy API và Portal.

## Runtime target

| Runtime | Adapter | Target local |
|---|---|---|
| Docker/Compose | `DockerRuntimeAdapter` | Ubuntu VM riêng qua SSH/Ansible |
| Kubernetes | `KubernetesRuntimeAdapter` | kind; namespaces `dev`, `staging`, `prod` |
| Systemd | `SystemdRuntimeAdapter` | Ubuntu VM thật có systemd qua SSH/Ansible |

## Portal trong browser thật

Unit test mock `netciClient`, nên chúng chứng minh component xử lý đúng **một câu trả lời
cho trước**. Chúng không chứng minh được Portal và API *đồng ý với nhau* về câu trả lời đó
— đổi tên field, đổi status code, hay một endpoint giờ đòi role đều pass unit test và fail
trước mặt người dùng. Đó là khoảng trống mà `frontend/e2e/` lấp.

```bash
make lab-up                                   # cần một netCI đang chạy thật
NETCI_API_URL=http://127.0.0.1:8100 npm --prefix frontend run test:e2e
```

Bao gồm cả **axe accessibility scan**. Lần chạy đầu tiên tìm ra 22 node vi phạm tương phản
màu WCAG AA — không phải lỗi mới, mà là lỗi chưa ai đo: các giá trị xám được viết cứng rải
rác trong CSS, trôi dần khỏi token `--muted`/`--subtle`. Đã sửa bằng cách gộp chúng về
token và làm token đủ đậm để đạt AA. Với một tập đoàn, accessibility thường là yêu cầu bắt
buộc khi mua sắm, nên đây không phải chi tiết thẩm mỹ.

## CI của chính netCI

netCI gate thay đổi của người khác; `.github/workflows/ci.yml` gate thay đổi của chính
netCI. Nó chạy profile `portable` trong `release-checklist.yaml` — **cùng một danh sách
check, gọi theo cùng một cách**, nên xanh trên CI và `make release-portable` xanh ở máy có
nghĩa giống hệt nhau. Không có danh sách bước thứ hai để lệch khỏi checklist.

```bash
make release-portable   # chính xác những gì job `portable` trên CI chạy
```

Ba job:

| Job | Chạy gì | Vì sao tách ra |
|---|---|---|
| `portable` | profile `portable` + pyflakes | chỉ cần Python và Node, chạy được ở mọi nơi |
| `database` | migrate từ DB rỗng, `--check-schema`, test durability, **backup + restore** | các test này **skip** khi không có PostgreSQL, nên CI là nơi duy nhất chúng chạy mọi lần |
| `portal-browser` | Playwright + axe, với API và PostgreSQL thật | unit test mock client nên không bắt được Portal và API lệch nhau |
| `supply-chain` | verifier chữ ký + OIDC với **cosign thật** | verifier tự skip khi thiếu cosign; thiếu job này thì suite xanh mà không chứng minh gì |

11 acceptance gate **không** nằm trên CI: chúng cần kind cluster, hai Jenkins controller,
registry và Backstage. Một workflow giả vờ chạy chúng chính là false green mà dự án này
sinh ra để chống. Chạy bằng `make release-ubuntu` trên host có lab.

## Các lệnh chính trên Ubuntu

```bash
make doctor          # gồm cả giới hạn fs.inotify và Python lib mà Ansible cần
make validate
make test
make lab-up          # PostgreSQL đã migrate + registry + API
make kind-up
make gates           # security-test, ba E2E và dora-dashboard

# Các gate cần Jenkins thật: hai controller dựng từ JCasC, git server và shared agent.
bash scripts/jenkins_lab.sh up
source .netci-gate/jenkins/lab.env
make jenkins-ci-loop jenkins-rebuild-gate failure-drill benchmark

# Gate cuối cần một Backstage instance thật.
bash scripts/backstage_lab.sh up
make backstage-test

make gates-all       # tất cả, khi cả hai lab đã chạy
make release-ubuntu
```

Sau khi sửa shared library hoặc `jenkins/casc/*.yaml`, dùng `bash scripts/jenkins_lab.sh publish` để controller đang chạy đọc lại config — không cần dựng lại từ đầu.

`make release-ubuntu` cố ý fail nếu một required gate vẫn có trạng thái `blocked` trong `release-checklist.yaml`. Hiện không còn gate nào blocked. Không đổi gate sang `ready` cho đến khi command chạy thật và kiểm tra evidence thật.

Xem [QUICKSTART](QUICKSTART.md) cho lab trên host không có bridge network, và [troubleshooting](docs/troubleshooting.md) cho các ràng buộc host mà thông báo lỗi không nói ra.

## Boundary

- Portal và Backstage chỉ gọi netCI API; không gọi Jenkins trực tiếp.
- Jenkins sở hữu checkout/test/build/SBOM/scan/sign/publish, không sở hữu application/deployment identity.
- netCI sở hữu policy, approval, audit, promotion, deploy, health check và rollback.
- Staging và production dùng cùng artifact digest; không rebuild khi promote.
- Core domain không import SDK/CLI của Jenkins, Temporal, Docker, Helm hoặc Systemd.
- Engine thật nằm sau seam có thể cấu hình: `NETCI_CI_MODE` (`none`|`jenkins`), `NETCI_CD_MODE` (`none`|`temporal`), `NETCI_AUTH_MODE` (`none`|`token`|`oidc`) và `NETCI_SIGNATURE_VERIFY_MODE` (`none`|`cosign`). `none` chỉ hợp lệ khi `NETCI_ENVIRONMENT=local`: netCI ghi nhận trạng thái và không giả vờ đã chạy build. Ngoài local, process fail-fast nếu auth, CI, CD, security evidence hoặc deploy-time signature verification bị tắt.
- **netCI tự kiểm chữ ký lúc deploy** khi bật `cosign`, thay vì tin vào boolean `signature.verified` do chính CI ghi về công việc của mình.
- **Actor luôn lấy từ credential đã xác thực**, không lấy từ request body. Vì vậy `actor`/`requestedBy`/`createdBy` đã bị bỏ khỏi API: một field mà server nhận rồi âm thầm bỏ qua còn tệ hơn là không có.
- State, source event, audit và log được ghi trong **cùng một transaction**, nên không thể có trường hợp trạng thái đã đổi nhưng DORA event bị mất.

## Xác thực và phân quyền

Mọi tuyên bố về governance của netCI chỉ có giá trị nếu platform biết ai đang gọi. Vì vậy
actor được lấy từ credential, và `require_environment_permission` — vốn được viết ra nhưng
chưa từng được gọi — nay là control thật.

```bash
# Local: không cần credential, nhưng netCI chỉ phục vụ loopback. Quên cấu hình xác thực
# không thể biến thành một netCI mở ra ngoài mạng.
NETCI_AUTH_MODE=none

# Pilot hoặc machine-to-machine: token lưu dưới dạng hash SHA-256.
NETCI_AUTH_MODE=token NETCI_AUTH_TOKENS_FILE=/etc/netci/tokens.yaml
python scripts/netci_token.py issue --subject dana --name "Dana Developer" --role developer

# Production: dùng identity provider của tập đoàn — người vào/ra theo directory.
NETCI_AUTH_MODE=oidc
NETCI_OIDC_ISSUER=https://login.microsoftonline.com/<tenant>/v2.0
NETCI_OIDC_AUDIENCE=api://netci
NETCI_OIDC_ROLE_MAP=netci-admins=platform-admin,release-managers=reviewer,engineers=developer
```

| Role | Được làm gì |
|---|---|
| `viewer` | đọc mọi thứ, không sửa gì |
| `developer` | tạo application/system, chạy pipeline dev/staging |
| `reviewer` | như developer, cộng thêm production: chạy, approve, reject |
| `platform-admin` | như reviewer, cộng quản trị platform; **không bị giới hạn theo team** |
| `pipeline` | chỉ báo *kết quả* build/deploy — không bao giờ cấp cho người |

Role nói **được làm loại việc gì**; team nói **được làm lên application nào**. Mỗi
application có `ownerTeam`, mỗi principal có danh sách team. Chỉ thành viên của team đó
(hoặc platform-admin) mới chạy được pipeline, approve deployment hay rollback của nó — đọc
thì không giới hạn, vì nhìn thấy trạng thái toàn hệ thống có ích và ít rủi ro hơn nhiều so
với hành động.

Application chưa có owner vẫn chạy như cũ, nên có thể áp dụng ownership dần. Khi mọi
application đã có owner, bật `NETCI_REQUIRE_APPLICATION_OWNER=true`: application không có
owner trở thành chỉ platform-admin dùng được, và application mới bắt buộc khai team.

Ba ràng buộc đáng chú ý:

- **Người tạo production run không được tự approve.** netCI so `pipeline_runs.started_by`
  với người approve và từ chối nếu trùng (`SEPARATION_OF_DUTIES`).
- **Endpoint máy là của máy.** platform-admin không post được CI result, và pipeline key
  không approve được deployment — cả hai chiều đều không có cửa sau.
- **`/healthz` báo `engines.auth`**, nên nhìn từ bên ngoài biết ngay hệ thống có bật xác
  thực hay không.

Chi tiết, gồm cả cách nối OIDC và các forgery mà verifier từ chối:
[security model](docs/security-model.md) và [ADR-011](docs/decisions/ADR-011-authentication-seam.md).

## Tài liệu

- [Giải thích kiến trúc hiện tại từ bài toán đến code chạy thật](docs/GIAI-THICH-KIEN-TRUC-HIEN-TAI.md) — tài liệu bắt đầu nên đọc để hiểu toàn bộ project và ranh giới “source hỗ trợ live”/“môi trường đã live”.

- [Architecture](docs/architecture.md)
- [Domain model](docs/domain-model.md)
- [API contract](docs/api-contract.md)
- [State machine](docs/state-machine.md)
- [Security model](docs/security-model.md)
- [DORA metrics](docs/dora-metrics.md)
- [Assumptions](docs/assumptions.md)
- [Troubleshooting](docs/troubleshooting.md)
- [Architecture decisions](docs/decisions/ADR-001-system-boundary.md)

## Không được coi là evidence hoàn thành

Static validator, screenshot đơn lẻ, file `sample.json`, command chỉ `echo`, hoặc file được tạo sau khi đặt biến `*_READY` không chứng minh E2E. Evidence nghiệm thu phải có command, commit SHA, application/run/deployment ID, artifact digest, timestamps, raw log/JSON và kết luận pass/fail.
