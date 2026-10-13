# K12 School Health Dashboards

Ứng dụng Flask gồm trang chọn 11 đối tượng, dashboard y tế tổng hợp và bảng hồ sơ chi tiết theo nhóm nghiệp vụ.

## Chạy ứng dụng

Mở PowerShell tại thư mục dự án:

```powershell
python -m pip install -r requirements.txt
$env:PGHOST = "localhost"
$env:PGPORT = "5432"
$env:PGUSER = "postgres"
$env:PGDATABASE = "K12_Staging_Local_Restore"
# Nếu PostgreSQL yêu cầu mật khẩu, đặt biến môi trường trước khi chạy:
# $env:PGPASSWORD = "<mật khẩu PostgreSQL>"
python app.py
```

Mở trang trình duyệt dữ liệu tại <http://127.0.0.1:5000/>. Chọn **Phân hệ Y tế** để vào trang chủ báo cáo và chọn dashboard: <http://127.0.0.1:5000/dashboard>. Trên trang chủ phân hệ Y tế, chọn **Quay lại trang dữ liệu** để trở về.

Trang chủ y tế có bộ lọc ngày/cơ sở, KPI lượt khám, số học sinh đã khám, BMI trung bình và số kết luận BMI; kèm biểu đồ tương tác lượt khám theo tháng, phân bố BMI và tình trạng huyết áp/nhịp tim. Di chuột để xem giá trị, bấm vào điểm tháng để lọc báo cáo; nút **Quay lại khoảng ngày** khôi phục khoảng ngày trước khi drill-down. Các biểu đồ theo vai trò cũng có nút này sau khi chọn điểm tháng. Bấm vào phân loại BMI hoặc chỉ số sinh tồn để xem số lượng.

## 11 dashboard theo đối tượng

| Đối tượng | Đường dẫn |
|---|---|
| Ban giám hiệu | `/dashboard/ban-giam-hieu` |
| Trưởng bộ phận y tế học đường | `/dashboard/truong-bo-phan-y-te` |
| Nhân viên y tế / y tá trường học | `/dashboard/nhan-vien-y-te` |
| Bác sĩ / đơn vị khám sức khỏe | `/dashboard/bac-si-don-vi-kham` |
| Phụ trách BHYT học sinh | `/dashboard/phu-trach-bhyt` |
| Điều phối tiêm chủng | `/dashboard/dieu-phoi-tiem-chung` |
| Phụ trách quản lý thuốc tại trường | `/dashboard/quan-ly-thuoc` |
| Giáo viên chủ nhiệm | `/dashboard/giao-vien-chu-nhiem` |
| Phụ huynh / người giám hộ | `/dashboard/phu-huynh` |
| Học sinh | `/dashboard/hoc-sinh` |
| Phòng công tác học sinh | `/dashboard/cong-tac-hoc-sinh` |

Đã bật cả 11 trang với chỉ số tổng hợp và bảng hồ sơ chi tiết theo nhóm nghiệp vụ của từng dashboard. Bảng chi tiết gồm tên học sinh, lớp, ngày, triệu chứng, chẩn đoán/ghi nhận và nội dung khám/BHYT/tiêm chủng/thuốc tương ứng. Giá trị liên hệ luôn hiển thị `***`; ứng dụng không truy vấn trường điện thoại, email hoặc địa chỉ để điền vào bảng này.

## Dữ liệu và bảo mật

Dashboard tổng hợp dữ liệu từ `profile_student` và các bảng `medical_*`. Biểu đồ và bảng tháng là tổng hợp; bảng hồ sơ trả dữ liệu chi tiết theo phạm vi nghiệp vụ của dashboard. Hồ sơ chi tiết mặc định giới hạn thời gian ở ba năm gần nhất, có lọc theo ngày/cơ sở/loại hồ sơ/từ khóa và phân trang.

**Cảnh báo quan trọng:** dữ liệu sức khỏe là dữ liệu nhạy cảm. Chọn dashboard chỉ thay đổi giao diện và loại hồ sơ, không xác thực danh tính hay đảm bảo quyền theo lớp/quan hệ giám hộ; người truy cập cục bộ có thể chọn vai trò khác. Trang trình duyệt dữ liệu ở `/` chỉ liệt kê/xem các bảng không nhận diện là dữ liệu cá nhân hoặc y tế; API dữ liệu, Power BI, CRUD và xuất CSV đều chặn các bảng có tên hoặc cột nhận diện nhạy cảm. Đây là biện pháp giảm rủi ro, không thay thế phân quyền server-side. Flask được cố định bind vào `127.0.0.1`; không đưa cổng này ra mạng, không dùng chung máy với người không được phép và không triển khai thực tế trước khi tích hợp SSO, phân quyền server-side và nhật ký truy cập. Chưa kết nối gửi thông báo phụ huynh hoặc luồng nhập/xuất Excel nghiệp vụ.

## API dashboard

- `/api/medical/dashboard?from=2024-01-01&to=2026-12-31&school_id=3` — số liệu y tế tổng hợp theo ngày và cơ sở.
- `/api/medical/records?role=nhan-vien-y-te&from=2024-01-01&to=2026-12-31` — hồ sơ theo nhóm nghiệp vụ của dashboard; `contact` được cố định là `***`.
