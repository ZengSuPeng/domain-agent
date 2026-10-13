def main(args, domain_dict):
    save_risk_report_to_txt(domain_dict, filename=r"D:\domain_report.txt")

def save_risk_report_to_txt(domain_dict, filename=r"D:\domain_report.txt"):
    """
    接收包含域名、理由及风险信息的字典，转换为运营易懂的自然语言报告并写入文本文件。
    
    Args:
        domain_dict: 字典格式，key为域名，value为包含风险信息的字典
                    例如: {"lacevy.com": {"risk_level": "低", "target_brand": "无", ...}}
        filename: 报告保存路径
    """
    with open(filename, "w", encoding="utf-8") as f:
        print("打开了文件")
        f.write("=" * 40 + "\n")
        f.write("📊 域名创意与风险筛查综合报告（运营简版）\n")
        f.write("=" * 40 + "\n\n")
        
        # 遍历字典中的每一个域名
        for domain, info in domain_dict.items():
            # 从 info 字典中获取信息，没有则给默认值
            reason = info.get("reason", "未检测到相关字段")
            risk_level = info.get("risk_level", "未检测到相关字段")
            target_brand = info.get("target_brand", "未检测到相关字段")
            negative_review = info.get("negative_review", "未检测到相关字段")
            
            # 匹配直观的图标提示
            if risk_level == "低":
                status_icon = "🟢 【安全】"
            elif risk_level == "中":
                status_icon = "🟡 【注意】"
            else:
                status_icon = "🔴 【高危·严禁使用】"
                
            # 写入文本
            f.write(f"🌐 待测域名: {domain}\n")
            f.write(f"   {status_icon} 风险评级: {risk_level}风险\n")
            f.write(f"   🏷️ 冲突品牌: {target_brand if target_brand != '无' else '未发现冲突品牌'}\n")
            f.write(f"   💡 创意理由: {reason}\n")
            f.write(f"   💬 负面评价: {negative_review}\n")
            f.write("-" * 40 + "\n\n")
            
    print(f"✅ 报告已成功写入本地文件：{filename}")