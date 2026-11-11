#pragma once

#include "runtime_types.hpp"
#include <cfenv>
#include <cctype>
#include <cmath>
#include <fstream>
#include <sstream>

// One execution engine for explicit scalar and tiled arithmetic graphs.
namespace circuit_ir {
using Config = std::map<int,circuit_profile::BaselineNode>;
inline void need(bool ok,const std::string& why) {
    if (!ok) throw std::runtime_error("Circuit IR: " + why);
}
inline double checked(double x) {
    need(std::isfinite(x) && std::abs(x)<=1099511627776.0,"nonfinite or oversized value"); return x;
}
inline double numeric(const cv::FileNode& n) {
    need(n.isInt() || n.isReal(),"expected number"); return checked(n.real());
}
inline int integer(const cv::FileNode& n) {
    const double x=numeric(n); need(std::trunc(x)==x && x>=-2147483648.0 && x<=2147483647.0,"expected signed 32-bit integer");
    return static_cast<int>(x);
}
inline bool boolean(const cv::FileNode& n) {
    const int x=integer(n); need(x==0 || x==1,"expected boolean"); return x!=0;
}
inline void typed(double x,bool integral,int width) {
    checked(x);
    if (integral) need(std::trunc(x)==x && (width==64 || (x>=-2147483648.0 && x<=2147483647.0)),"integer value exceeds declared width or is fractional");
}
// FileStorage stores JSON integer tokens in signed 32 bits. Force numeric
// tokens through its real parser before any schema or range checks.
inline std::string realNumbers(const std::string& json) {
    std::string out; out.reserve(json.size());
    for(size_t i=0;i<json.size();) {
        if(json[i]=='"') {
            out+=json[i++];
            while(i<json.size()) {
                const char c=json[i++]; out+=c;
                if(c=='\\' && i<json.size()) out+=json[i++];
                else if(c=='"') break;
            }
        } else if(json[i]=='-' || std::isdigit(static_cast<unsigned char>(json[i]))) {
            const size_t start=i++;
            while(i<json.size() && (std::isdigit(static_cast<unsigned char>(json[i])) || json[i]=='.' || json[i]=='e' || json[i]=='E' || json[i]=='+' || json[i]=='-')) ++i;
            const auto token=json.substr(start,i-start); out+=token;
            if(token.find_first_of(".eE")==std::string::npos) out+=".0";
        } else out+=json[i++];
    }
    return out;
}
struct Operand {
    int index=-1; double constant=0;
    double get(const std::vector<double>& values) const { return index<0 ? constant : values.at(index); }
};
struct Input { int dx=0,dy=0; };
struct Operation {
    std::string id,op;
    std::vector<Operand> args;
    bool integral=false;
    int width=64,group=-1,bits=0;
    double factor=1,minimum=0,maximum=255;
};
struct Output { Operand value; int dx,dy; };
struct RoundingScope {
    int previous=std::fegetround();
    RoundingScope() { need(std::fesetround(FE_TONEAREST)==0,"cannot set nearest-even rounding"); }
    ~RoundingScope() { std::fesetround(previous); }
};
struct Profile {
    std::string name;
    Config baseline;
    std::vector<Input> inputs;
    std::vector<Operation> operations;
    std::vector<Output> outputs;
    int height=1,width=1;
    bool reflect=false,interior=false,borderInput=true;

    static Profile load(const std::string& path) {
        std::ifstream file(path); need(file.good(),"cannot open "+path);
        std::ostringstream text; text<<file.rdbuf(); return parse(text.str());
    }
    static Profile parse(const std::string& json) {
        cv::FileStorage storage(realNumbers(json),cv::FileStorage::READ|cv::FileStorage::MEMORY|cv::FileStorage::FORMAT_JSON);
        need(storage.isOpened(),"cannot parse JSON");
        const auto root=storage.root();
        need(integer(root["schema"])==3,"expected schema 3");
        need(circuit_profile::word(root["application"])=="ir","expected application ir");
        Profile p; p.name=circuit_profile::word(root["name"]); need(!p.name.empty(),"empty name");
        const auto base=root["baseline"]; need(base.isSeq(),"baseline must be array");
        for (const auto& b:base) {
            const int group=integer(b["node"]); need(group>=0,"negative unit group");
            need(p.baseline.emplace(group,circuit_profile::unit(circuit_profile::word(b["unit"]))).second,"duplicate unit group");
        }
        const auto ir=root["ir"]; need(ir.isMap() && integer(ir["schema"])==1,"expected IR schema 1");
        const auto tile=ir["tile"]; need(tile.isSeq() && tile.size()==2,"tile must be [height,width]");
        p.height=integer(tile[0]); p.width=integer(tile[1]);
        need(p.height>0 && p.width>0 && p.height<=256 && p.width<=256,"invalid tile dimensions");
        const auto border=circuit_profile::word(ir["border"]);
        need(border=="replicate" || border=="reflect101","unsupported border"); p.reflect=border=="reflect101";
        p.interior=boolean(ir["interior_only"]);
        if (!ir["border_output"].empty()) {
            const auto out=circuit_profile::word(ir["border_output"]);
            need(out=="zero" || out=="input","invalid border output"); p.borderInput=out=="input";
        }
        std::map<std::string,int> names;
        const auto operand=[&](const cv::FileNode& n) -> Operand {
            if(n.isString()) {
                const auto id=circuit_profile::word(n); const auto it=names.find(id);
                need(it!=names.end(),"unknown or forward value reference: "+id); return {it->second,0};
            }
            return {-1,numeric(n)};
        };
        const auto addName=[&](const std::string& id) {
            need(!id.empty() && id!="-1" && names.emplace(id,static_cast<int>(names.size())).second,"duplicate or invalid value ID");
        };
        const auto in=ir["inputs"]; need(in.isSeq() && in.size()>0 && in.size()<=100000,"inputs must be nonempty array");
        for(const auto& i:in) {
            addName(circuit_profile::word(i["id"]));
            const int dx=integer(i["dx"]),dy=integer(i["dy"]);
            need(std::abs(static_cast<long long>(dx))<=100000 && std::abs(static_cast<long long>(dy))<=100000,"input offset too large");
            p.inputs.push_back({dx,dy});
        }
        std::set<int> usedGroups;
        const auto ops=ir["operations"]; need(ops.isSeq() && ops.size()<=100000,"operations must be array");
        for(const auto& o:ops) {
            Operation op; op.id=circuit_profile::word(o["id"]); op.op=circuit_profile::word(o["op"]);
            const auto& kind=op.op;
            need(kind=="add" || kind=="sub" || kind=="scale" || kind=="abs" || kind=="shift" || kind=="round_shift" || kind=="round_even" || kind=="trunc" || kind=="clamp","unsupported operation");
            op.integral=boolean(o["integer"]); op.width=integer(o["width"]);
            need((op.width==32 || op.width==64) && boolean(o["signed"]),"only signed 32/64-bit operations supported");
            const auto args=o["args"];
            need(args.isSeq() && args.size()==((kind=="add" || kind=="sub")?2u:1u),"wrong argument count");
            for(const auto& a:args) op.args.push_back(operand(a));
            if (!o["unit_node"].empty()) {
                op.group=integer(o["unit_node"]);
                need(op.integral && (kind=="add" || kind=="sub") && p.baseline.count(op.group),"invalid approximate operation or unit group");
                usedGroups.insert(op.group);
            }
            if(kind=="scale") op.factor=numeric(o["factor"]);
            if(kind=="shift" || kind=="round_shift") {
                op.bits=integer(o["bits"]); need(op.bits>=0 && op.bits<=63,"shift bits out of range");
            }
            if(kind=="clamp") {
                op.minimum=numeric(o["min"]); op.maximum=numeric(o["max"]);
                need(op.minimum<=op.maximum,"reversed clamp range");
            }
            addName(op.id); p.operations.push_back(std::move(op));
        }
        need(usedGroups.size()==p.baseline.size(),"unused baseline group");
        const auto out=ir["outputs"]; need(out.isSeq() && out.size()==static_cast<size_t>(p.height*p.width),"outputs must completely cover tile");
        std::set<std::pair<int,int>> positions;
        for(const auto& o:out) {
            const int dx=integer(o["dx"]),dy=integer(o["dy"]);
            need(dx>=0 && dx<p.width && dy>=0 && dy<p.height && positions.emplace(dx,dy).second,"invalid or duplicate output offset");
            p.outputs.push_back({operand(o["value"]),dx,dy});
        }
        return p;
    }
    Config exactConfig() const { auto result=baseline; for(auto& b:result) b.second.exact=true; return result; }
    Config compensatedConfig(const std::vector<int>& replacements) const {
        auto result=baseline;
        for(int group:replacements) {
            const auto it=result.find(group);
            need(it!=result.end() && !it->second.exact,"replacement must name an original approximate group once");
            it->second.exact=true;
        }
        return result;
    }
    bool stencilInside(const cv::Mat& image,int x,int y) const {
        for(const auto& in:inputs)
            if(x+in.dx<0 || x+in.dx>=image.cols || y+in.dy<0 || y+in.dy>=image.rows) return false;
        return true;
    }
    cv::Mat run(const cv::Mat& image,const Config& config,const cv::Rect& roi={},const std::string& site="",int bit=0) const {
        need(!image.empty() && image.type()==CV_8UC1 && image.rows<=100000 && image.cols<=100000 && image.total()<=100000000,"unsupported image dimensions or type");
        need(config.size()==baseline.size(),"invalid unit configuration");
        for(const auto& b:baseline) need(config.count(b.first),"missing unit configuration");
        const bool flip=!site.empty();
        if(flip) {
            need(bit>=0 && bit<12,"bit must be non-sign bit in [0,11]");
            const auto found=std::find_if(operations.begin(),operations.end(),[&](const Operation& o){return o.id==site;});
            need(found!=operations.end() && found->integral,"flip site must be an integer operation");
        }
        need(roi.x>=0 && roi.y>=0 && roi.width>=0 && roi.height>=0 && static_cast<long long>(roi.x)+roi.width<=image.cols && static_cast<long long>(roi.y)+roi.height<=image.rows,"invalid region");
        RoundingScope rounding;
        cv::Mat result(image.size(),CV_8UC1,cv::Scalar(0));
        std::vector<double> values; values.reserve(inputs.size()+operations.size());
        for(int y=0;y<image.rows;y+=height) for(int x=0;x<image.cols;x+=width) {
            const bool inside=stencilInside(image,x,y);
            if(interior && !inside) {
                if(borderInput) for(const auto& out:outputs) if(x+out.dx<image.cols && y+out.dy<image.rows)
                    result.at<unsigned char>(y+out.dy,x+out.dx)=image.at<unsigned char>(y+out.dy,x+out.dx);
                continue;
            }
            const bool active=flip && x>=roi.x && y>=roi.y && x+width<=roi.x+roi.width && y+height<=roi.y+roi.height;
            values.clear();
            for(const auto& in:inputs) {
                const int sx=cv::borderInterpolate(x+in.dx,image.cols,reflect?cv::BORDER_REFLECT_101:cv::BORDER_REPLICATE);
                const int sy=cv::borderInterpolate(y+in.dy,image.rows,reflect?cv::BORDER_REFLECT_101:cv::BORDER_REPLICATE);
                values.push_back(image.at<unsigned char>(sy,sx));
            }
            for(const auto& op:operations) {
                const double a=op.args[0].get(values);
                const double b=op.args.size()>1 ? op.args[1].get(values):0;
                double v=0;
                if(op.op=="add" || op.op=="sub") {
                    if(op.group>=0 && !config.at(op.group).exact) {
                        const double rhs=op.op=="sub" ? -b:b;
                        need(std::trunc(a)==a && std::trunc(rhs)==rhs && a>=-2048 && a<=2047 && rhs>=-2048 && rhs<=2047,"approximate operands exceed signed 12-bit width");
                        v=approximate::addSigned12(static_cast<int>(a),static_cast<int>(rhs),config.at(op.group).unit);
                    } else v=op.op=="add"?a+b:a-b;
                } else if(op.op=="scale") v=a*op.factor;
                else if(op.op=="abs") v=std::abs(a);
                else if(op.op=="shift") v=std::floor(std::ldexp(a,-op.bits));
                else if(op.op=="round_shift") v=std::round(std::ldexp(a,-op.bits));
                else if(op.op=="round_even") v=std::nearbyint(a);
                else if(op.op=="trunc") v=std::trunc(a);
                else v=std::max(op.minimum,std::min(op.maximum,a));
                typed(v,op.integral,op.width);
                if(active && op.id==site) {
                    need(v>=-4096 && v<=4095,"flip site exceeds signed 13-bit output");
                    v=core::flipNonSignOutputBit(static_cast<int>(v),bit); typed(v,op.integral,op.width);
                }
                values.push_back(v);
            }
            for(const auto& out:outputs) if(x+out.dx<image.cols && y+out.dy<image.rows) {
                const double v=checked(out.value.get(values));
                result.at<unsigned char>(y+out.dy,x+out.dx)=static_cast<unsigned char>(std::nearbyint(std::max(0.0,std::min(255.0,v))));
            }
        }
        return result;
    }
};
}
