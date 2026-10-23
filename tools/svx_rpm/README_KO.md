# SVX RPM 동반 프로그램 (읽기 전용, 선택 사항)

콤마에서 엔진 RPM(EMS11 0x316)을 **읽기만** 해서, 같은 Wi-Fi에 있는 폰의 SVX(셀토스 가상배기음) 앱으로
인증된 UDP 패킷(SVX1, 40바이트)을 보냅니다.

* **기본은 꺼져 있습니다.** 직접 실행할 때만 동작합니다. 부팅·manager·onroad 자동 시작 코드가 없습니다.
* 기존 `can` 서비스를 **구독만** 하고 `CarParams` 값을 **읽기만** 합니다.
  * 다음은 전혀 하지 않습니다: 퍼블리셔 생성, sendcan, Panda/USB/socketcan 접근, 진단(OBD/UDS) 요청, Params 쓰기.
  * 조향·제동·가속·모델 코드는 바꾸지 않았습니다. 변경은 이 폴더(`tools/svx_rpm/`)뿐입니다.
* 실제 RPM을 그대로 보냅니다. "4,000 rpm에서 최고 소리" 같은 대응은 앱 안에서만 합니다.

## 지원 범위 (이 밖은 거부)
* 프로파일 `kia_seltos_2021_ems11` 하나뿐입니다.
* 조건: openpilot 차량 선택이 `KIA_SELTOS`(hyundai, **CAN-FD 아님**)이고, CAN **bus 0**이어야 합니다.
* 이 조건이 아니면 실행 직후 무효 패킷 1개를 보내고 종료합니다 (종료 코드 3).
  * `CarParams`가 아직 없을 때는 무효 상태로 기다리기만 하고, CAN 데이터는 쓰지 않습니다.
* 레이아웃 근거: 이 저장소의 opendbc `hyundai_kia_generic.dbc`에 정의된 `EMS11` `N`입니다.
  * 시작 비트 16, 길이 16, 리틀엔디언, 단위 0.25 rpm입니다.
* `carState.engineRpm`은 쓰지 않습니다. 현대/기아 carstate가 이 값을 채우지 않기 때문입니다.
* **실제 차량에서 EMS11이 bus 0에 나오는지는 확인되지 않았습니다.** 연식·엔진·하네스에 따라 다를 수 있으니 아래 `scan`으로 먼저 확인하세요.

## 설치 (운전용 브랜치를 바꾸지 않는 방법)
아래는 안내일 뿐이며, 이 저장소의 작업에서 차량에 실제로 실행한 적은 없습니다.
현재 브랜치와 작업 트리는 바뀌지 않습니다. 별도 ref 하나(`refs/svx-rpm/import`)만 추가됩니다.

**1단계: 가져오기 + 커밋 확인.**
```sh
git -C /data/openpilot fetch https://github.com/tjheo/openpilot.git +refs/heads/feat/svx-rpm-sidecar:refs/svx-rpm/import && git -C /data/openpilot rev-parse refs/svx-rpm/import
```
출력된 SHA가 GitHub `feat/svx-rpm-sidecar` 브랜치의 최신 커밋과 **같을 때만** 2단계로 갑니다.
fetch가 실패하면 SHA가 나오지 않으니, 그때는 멈추세요.

**2단계: 이 폴더만 꺼내기.** `/data/svx_rpm/app`나 `app.new`가 이미 있으면 아무것도 하지 않고 멈춥니다.
```sh
bash -c 'set -euo pipefail
D=/data/svx_rpm
test ! -e "$D/app"; test ! -e "$D/app.new"
mkdir -p "$D/app.new"; chmod 700 "$D"
git -C /data/openpilot archive refs/svx-rpm/import tools/svx_rpm | tar -x -C "$D/app.new" --strip-components=2
test -f "$D/app.new/svx_rpm.py"
mv "$D/app.new" "$D/app"
echo OK'
```
* `OK`가 나와야 성공입니다.
* 설정 파일은 코드 폴더 **밖**(기본 `/data/svx_rpm/config.json`)에 두어야 합니다. 프로그램이 이를 확인합니다.

## 설정 (한 번)
1. 폰을 핫스팟으로 쓰거나, 콤마와 폰을 같은 Wi-Fi에 연결합니다.
2. **폰의 IP**를 확인합니다. 폰이 핫스팟이면 보통 콤마의 기본 게이트웨이가 폰입니다: `ip -4 route show default`
3. **콤마의 IP**를 확인합니다: `ip -4 addr show wlan0`
4. 설정 파일을 만듭니다. 권한 600으로 생성되고, 32바이트 난수 키가 새로 만들어집니다.
   ```sh
   python3 /data/svx_rpm/app/svx_rpm.py init --target <폰 IP>
   ```
   * 포트는 기본 47816입니다. 바꾸려면 `--port <번호>`를 붙이고, 앱의 포트도 같게 맞춥니다.
   * 키는 명령줄 인자로 넘기지 않으므로 쉘 기록에 남지 않습니다.
   * 화면에 64자 키가 한 번 표시됩니다. 다시 보려면 `python3 /data/svx_rpm/app/svx_rpm.py show-key`
5. 앱 설정에 다음을 입력합니다.
   * 콤마 IP 칸: 3번에서 확인한 **콤마** IP (폰 IP가 아닙니다)
   * 포트: 47816 (`init`에서 다른 포트를 지정했다면 그 번호)
   * 페어링 키: 64자 그대로
   * 키는 32바이트 난수를 64자 16진수 글자로 쓴 것입니다. 앱은 입력한 글자 자체(64바이트 ASCII)로 HMAC을 계산하므로, 대소문자나 공백 없이 정확히 입력해야 합니다.
6. 확인: `python3 /data/svx_rpm/app/svx_rpm.py check` (키는 가려서 표시됩니다)
7. 폰 IP가 바뀌었을 때:
   * 편집기로 `config.json`의 `target_ip`만 고칩니다. 권한 600은 그대로 유지됩니다.
   * `check`로 확인합니다.
   * 실행 중인 `run`을 정지했다가 다시 시작합니다. 키는 그대로 씁니다.

* 대상 주소는 사설 LAN 유니캐스트만 허용합니다: 10/8, 172.16/12, 192.168/16, 끝자리 0·255 제외.
* 인터넷, 브로드캐스트, 멀티캐스트 주소는 거부합니다.
* 인증 없이 보내는 모드는 없습니다.

## 사용 순서 (정차 상태에서 먼저)
```sh
python3 /data/svx_rpm/app/svx_rpm.py scan --seconds 10      # 버스별 EMS11 수신 확인 (읽기 전용)
python3 /data/svx_rpm/app/svx_rpm.py run --dry-run --duration 20   # 보내지 않고 1초마다 출력
python3 /data/svx_rpm/app/svx_rpm.py run                     # 실제 전송, Ctrl+C로 정지
```
* `scan`에서 `src 0 <- profile bus` 줄을 확인합니다.
  * 프레임이 꾸준한 주기로 들어오고, 공회전 rpm이 그럴듯해야 합니다. 정확한 주기는 실차에서 확인된 값이 없습니다.
  * 이 줄이 없으면 이 차에서는 쓸 수 없습니다.
* `run` 전에 앱에서 **볼륨을 낮게** 두고 [실시간 (콤마)]을 누르세요. 신호가 없으면 앱은 "콤마 연결 대기"로 소리 없이 기다립니다.
* SSH를 끊어도 계속 돌리려면 tmux를 씁니다:
  ```sh
  tmux new -d -s svx 'python3 /data/svx_rpm/app/svx_rpm.py run'
  ```
* 로그(stderr)에는 키가 나오지 않습니다.

## 정지 / 비활성화 / 제거(롤백)
1. 정지: 실행 중인 창에서 Ctrl+C를 누릅니다. tmux로 실행했다면 `tmux send-keys -t svx C-c`를 입력합니다.
   * SIGTERM/SIGINT를 받으면 소켓을 닫고 끝납니다.
   * 개발 PC 테스트에서는 1초 안에 끝났습니다. 콤마에서의 실제 시간과, 앱의 버퍼·페이드로 소리가 멈추는 시간은 확인하지 않았습니다.
2. 확인: `pgrep -af 'svx_rpm.py run' || echo stopped` 의 출력이 `stopped`여야 합니다.
3. 비활성화/롤백: 폴더를 지우지 말고 옮겨 둡니다. 이 폴더에는 키가 들어 있으니, 필요 없으면 나중에 직접 지우세요.
   ```sh
   test ! -e /data/svx_rpm.disabled && mv /data/svx_rpm /data/svx_rpm.disabled && git -C /data/openpilot update-ref -d refs/svx-rpm/import
   ```
* openpilot 파일과 브랜치를 바꾸지 않았으므로, 롤백은 프로세스 정지와 폴더 이동으로 끝납니다.
* 키를 바꾸려면 `config.json`을 지우고 `init`을 다시 실행한 뒤, 앱에도 새 키를 입력합니다.

## 동작 규칙
* 루프는 50Hz입니다. 실행 우선순위는 nice 19입니다.
* 한 주기에 최대 512개 메시지만 꺼내고, 가장 최근 8개만 해석합니다.
* 데이터 패킷은 최대 50Hz로, 새 프레임이 없으면 200ms마다 하트비트를 보냅니다.
* 프레임의 나이는 `logMonoTime`(pandad 수신 시각, CLOCK_BOOTTIME) 기준입니다. 큐에서 꺼낸 시각이 아닙니다.
* 다음 프레임은 사용하지 않습니다:
  * 미래 시각, 0 이하 시각, 순서가 뒤바뀌거나 중복된 프레임
  * 300ms보다 오래된 프레임
  * 길이가 8바이트가 아닌 프레임
  * 다른 버스나 TX 에코에서 온 프레임
  * 더 최근의 `can` 이벤트가 `valid=false`로 표시된 경우
* 다음 경우에는 RPM_VALID 플래그를 끕니다: 프레임 없음, 데이터가 오래됨, RPM 0(시동 꺼짐), F_N_ENG 고장 비트, 0xFFFF, 9,000rpm 초과.
* 앱 쪽 판정:
  * 새 프레임이 300ms 동안 없으면 STALE(무음 쪽)로 판단합니다.
  * 패킷이 1초 동안 오지 않으면 LINK_LOST로 판단합니다.
  * 무음이 되기까지의 실제 시간은 앱의 페이드 설정에 따르며, 실차에서 측정하지 않았습니다.
* 차량 선택이 사라지면(CarParams가 없어지거나 바뀌면) 데이터를 버리고 **새 세션**을 시작합니다.
  * 앱은 새 세션의 첫 패킷을 근거로 쓰지 않으므로, 그 패킷을 받으면 소리를 멈추는 쪽으로 갑니다. 페이드 시간은 확인하지 않았습니다.
  * CarParams는 2초마다 확인하므로, 이를 알아채기까지 최대 약 2초 걸릴 수 있습니다.
* 네트워크 오류가 나도 같은 주기로 계속 실행합니다. 재전송 폭주는 없고, 오류 로그는 10초에 한 번만 남깁니다.

## 한계
* 실제 차량·폰에서 실행해 보지 않았습니다 (VERIFICATION.md 참고). EMS11의 존재, 버스, 지연 시간은 실차 확인이 필요합니다.
* 이 서버에는 msgq가 빌드되어 있지 않아서, 실제 `can` 소켓 구독은 테스트하지 못했습니다.
  * 실제 cereal 스키마로 만든 이벤트를 넣는 방식으로만 검증했습니다.
* 같은 셀토스라도 CAN-FD 차종이거나 다른 플랫폼으로 인식되면 거부합니다. 이는 의도한 동작입니다.

## 출처와 라이선스
* 프로토콜, EMS11 디코더, 송신 로직, 테스트 벡터(`tests/fixtures/*.json`, 원본과 바이트 단위로 같음)는 저장소 소유자 본인의 SVX 프로젝트에서 가져와 이 저장소에 맞게 고쳤습니다.
  * 원래 SVX 프로젝트에는 LICENSE 파일이 없습니다.
  * 이 폴더는 소유자의 승인에 따라 이 저장소의 라이선스(MIT, 저장소 루트 `LICENSE`)로 공개됩니다.
  * 원래 프로젝트가 MIT였다는 뜻은 아닙니다.
* EMS11 신호 정의는 opendbc(MIT, `opendbc_repo/LICENSE`)의 `hyundai_kia_generic.dbc`를 참조했습니다. DBC 파일은 복사하지 않았습니다.
* 제3자 코드, 음원, 바이너리, 키는 포함되어 있지 않습니다.
